<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { apiRequest, RequestError } from '../api/common'

const props = defineProps<{
  sessionId: string
  turnId: string
  answer: string
  disabled: boolean
  configured?: boolean
}>()
const emit = defineEmits<{ apply: [text: string]; 'microphone-active': [active: boolean]; 'draft-active': [active: boolean] }>()

type Transcription = {
  request_id: string
  turn_id: string
  status: 'running' | 'ready' | 'failed' | 'expired'
  transcript: string | null
  error_message: string | null
}
type Phase = 'idle' | 'permission' | 'recording' | 'preparing' | 'waiting' | 'unknown' | 'failed' | 'review'
const MAX_SECONDS = 180
const SAMPLE_RATE = 16_000
const phase = ref<Phase>('idle')
watch(phase, (value) => emit('draft-active', value !== 'idle'), { flush: 'sync' })
const seconds = ref(0)
const candidate = ref('')
const notice = ref('')
const canResend = ref(false)
const supported = Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder
  && window.OfflineAudioContext)
const answerWithCandidate = computed(() => props.answer.trim()
  ? `${props.answer}\n\n${candidate.value}` : candidate.value)
const busy = computed(() => ['permission', 'preparing', 'waiting'].includes(phase.value))
const clock = computed(() => `${Math.floor(seconds.value / 60)}:${String(seconds.value % 60).padStart(2, '0')}`)
watch(
  () => phase.value === 'permission' || phase.value === 'recording',
  (active) => emit('microphone-active', active),
  { flush: 'sync' },
)

const base = `/interviews/${props.sessionId}/transcriptions`
const storageKey = `probe:voice:${props.sessionId}`
let recorder: MediaRecorder | null = null
let stream: MediaStream | null = null
let recordingTimer: number | undefined
let pollTimer: number | undefined
let requestId = ''
let pendingAudio: Blob | null = null
let generation = 0
let activeRequest: symbol | null = null

function remember(id: string) {
  try {
    if (id) sessionStorage.setItem(storageKey, JSON.stringify({ id, turn: props.turnId }))
    else sessionStorage.removeItem(storageKey)
  } catch { /* 关闭会话存储后，当前页仍可恢复连接。 */ }
}

function releaseRecording() {
  window.clearInterval(recordingTimer)
  const running = recorder
  recorder = null
  if (running && running.state !== 'inactive') running.stop()
  stream?.getTracks().forEach((track) => track.stop())
  stream = null
}

function reset(forget = true) {
  generation += 1
  activeRequest = null
  releaseRecording()
  window.clearTimeout(pollTimer)
  if (forget && requestId) remember('')
  requestId = ''
  pendingAudio = null
  candidate.value = ''
  notice.value = ''
  canResend.value = false
  phase.value = 'idle'
}

// 先让浏览器解码自己的容器，再按百炼接收的统一 WAV 规格编码。
async function encodeWav(audio: Blob): Promise<Blob> {
  const context = new OfflineAudioContext(1, 1, SAMPLE_RATE)
  const decoded = await context.decodeAudioData(await audio.arrayBuffer())
  const samples = decoded.getChannelData(0)
  const count = Math.min(samples.length, MAX_SECONDS * SAMPLE_RATE)
  const view = new DataView(new ArrayBuffer(44 + count * 2))
  const tag = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i))
  }
  tag(0, 'RIFF')
  view.setUint32(4, 36 + count * 2, true)
  tag(8, 'WAVE')
  tag(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, SAMPLE_RATE, true)
  view.setUint32(28, SAMPLE_RATE * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  tag(36, 'data')
  view.setUint32(40, count * 2, true)
  let peak = 0
  for (let i = 0; i < count; i++) {
    const value = Math.max(-1, Math.min(1, samples[i]))
    peak = Math.max(peak, Math.abs(value))
    view.setInt16(44 + i * 2, value * (value < 0 ? 32768 : 32767), true)
  }
  if (peak < 0.002) throw new Error('没有检测到声音，请检查麦克风后重录。')
  return new Blob([view.buffer], { type: 'audio/wav' })
}

function applyResult(job: Transcription, id: string, token: number) {
  if (token !== generation || job.request_id !== id || job.turn_id !== props.turnId) return
  if (job.status === 'running') {
    phase.value = 'waiting'
    notice.value = '正在转写，回答尚未提交。'
    pollTimer = window.setTimeout(() => void readStatus(), 1_500)
  } else if (job.status === 'ready' && job.transcript) {
    pendingAudio = null
    candidate.value = job.transcript
    notice.value = '转写完成，使用后可以在回答框里修改。'
    phase.value = 'review'
  } else if (pendingAudio && (job.status === 'failed' || job.status === 'expired')) {
    phase.value = 'failed'
    notice.value = `${job.error_message || '转写未完成。'} 本页已保留原录音。`
  } else {
    reset()
    notice.value = job.error_message || '未得到可用的转写，可以重录或继续打字。'
  }
}

async function readStatus() {
  window.clearTimeout(pollTimer)
  const id = requestId
  if (!id || props.disabled || activeRequest) return
  const request = Symbol()
  activeRequest = request
  const token = generation
  phase.value = 'waiting'
  try {
    applyResult(await apiRequest<Transcription>(`${base}/${id}`, undefined, 8_000), id, token)
  } catch (error) {
    if (token !== generation) return
    const missing = error instanceof RequestError && error.status === 404
    if (missing && !pendingAudio) {
      reset()
      notice.value = '找不到原录音任务，可以重录或继续打字。'
      return
    }
    canResend.value = missing
    phase.value = 'unknown'
    notice.value = missing ? '服务器还没有收到这段录音，可以按原请求重新上传。'
      : '暂时读不到转写状态，可以再读取一次或继续打字。'
  } finally {
    if (activeRequest === request) activeRequest = null
  }
}

async function upload() {
  const id = requestId
  const audio = pendingAudio
  if (!id || !audio || props.disabled || activeRequest) return
  const request = Symbol()
  activeRequest = request
  const token = generation
  phase.value = 'waiting'
  canResend.value = false
  notice.value = '正在上传录音，回答尚未提交。'
  const query = new URLSearchParams({ request_id: id, turn_id: props.turnId })
  try {
    const job = await apiRequest<Transcription>(`${base}?${query}`, {
      method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: audio,
    }, 20_000)
    applyResult(job, id, token)
  } catch (error) {
    if (token !== generation) return
    if (error instanceof RequestError && (error.status === 0 || error.status >= 500)
      && error.code !== 'transcription_not_configured') {
      phase.value = 'unknown'
      notice.value = '没收到上传确认，先读取原任务，避免重复转写。'
    } else if (error instanceof RequestError && error.status === 409) {
      phase.value = 'unknown'
      notice.value = '当前状态暂不能转写，录音已保留。恢复作答后先读取原任务状态。'
    } else {
      reset()
      notice.value = error instanceof Error ? error.message : '未能上传录音。'
    }
  } finally {
    if (activeRequest === request) activeRequest = null
  }
}

function retryAudio() {
  if (phase.value !== 'failed' || !pendingAudio || props.disabled || activeRequest) return
  if (!window.confirm('用原录音发起一次新的转写？语音服务可能再次计费。')) return
  generation += 1
  requestId = crypto.randomUUID()
  remember(requestId)
  void upload()
}

async function prepare(audio: Blob, token: number) {
  phase.value = 'preparing'
  notice.value = '正在准备录音…'
  try {
    const wav = await encodeWav(audio)
    if (token !== generation) return
    pendingAudio = wav
    requestId = crypto.randomUUID()
    remember(requestId)
    if (props.disabled) {
      phase.value = 'unknown'
      canResend.value = true
      notice.value = '录音已保留，恢复作答后可上传转写。'
      return
    }
    await upload()
  } catch (error) {
    if (token !== generation) return
    reset()
    notice.value = error instanceof Error && error.message.startsWith('没有检测到')
      ? error.message : '浏览器无法处理这段录音，可以重录或继续打字。'
  }
}

function stopRecording() {
  if (recorder?.state === 'recording') {
    phase.value = 'preparing'
    releaseRecording()
  }
}

async function startRecording() {
  if (!supported || !props.configured || props.disabled || phase.value !== 'idle') return
  const token = ++generation
  phase.value = 'permission'
  notice.value = '等待麦克风权限…'
  seconds.value = 0
  try {
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true })
    if (token !== generation || props.disabled) {
      mic.getTracks().forEach((track) => track.stop())
      return
    }
    stream = mic
    const mimeType = ['audio/webm;codecs=opus', 'audio/mp4'].find((value) => MediaRecorder.isTypeSupported(value))
    const capture = new MediaRecorder(mic, mimeType ? { mimeType } : undefined)
    recorder = capture
    const chunks: Blob[] = []
    capture.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data) }
    capture.onstop = () => {
      if (token !== generation) return
      releaseRecording()
      if (chunks.length) void prepare(new Blob(chunks, { type: capture.mimeType }), token)
      else {
        reset()
        notice.value = '这次没有录到声音，请重录或继续打字。'
      }
    }
    capture.onerror = () => {
      if (token !== generation) return
      reset()
      notice.value = '录音中断，请检查麦克风，或继续打字。'
    }
    capture.start()
    phase.value = 'recording'
    notice.value = '正在录音。'
    const started = performance.now()
    recordingTimer = window.setInterval(() => {
      seconds.value = Math.min(MAX_SECONDS, Math.floor((performance.now() - started) / 1000))
      if (seconds.value >= MAX_SECONDS) stopRecording()
    }, 250)
  } catch (error) {
    if (token !== generation) return
    reset()
    notice.value = error instanceof DOMException && error.name === 'NotAllowedError'
      ? '未获得麦克风权限，可在浏览器中允许后重试，或继续打字。'
      : '无法开始录音，请检查麦克风是否可用，或继续打字。'
  }
}

function useCandidate() {
  if (props.disabled || !candidate.value || answerWithCandidate.value.length > 8_000) return
  emit('apply', answerWithCandidate.value)
  reset()
}

function releaseOnExit() { reset(false) }
watch(() => props.disabled, (disabled) => {
  if (disabled) {
    if (phase.value === 'recording') stopRecording()
    else if (phase.value === 'permission') reset()
  } else if (requestId && phase.value === 'waiting') {
    void readStatus()
  }
}, { flush: 'sync' })
onMounted(() => {
  window.addEventListener('pagehide', releaseOnExit)
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) ?? 'null') as { id: string; turn: string } | null
    if (saved?.turn === props.turnId && typeof saved.id === 'string') {
      requestId = saved.id
      phase.value = 'waiting'
      if (!props.disabled) void readStatus()
    }
  } catch { /* 无会话存储时只保留本页状态。 */ }
})
onBeforeUnmount(() => {
  window.removeEventListener('pagehide', releaseOnExit)
  releaseOnExit()
})
</script>

<template>
  <section class="voice-answer" aria-label="语音作答">
    <div class="voice-controls">
      <button v-if="phase === 'idle'" class="button button-outline button-small" type="button"
        :disabled="disabled || !supported || !configured" @click="startRecording">开始录音</button>
      <button v-if="phase === 'recording'" class="button button-primary button-small" type="button"
        @click="stopRecording">停止并转写</button>
      <span v-if="phase === 'recording'" class="voice-recording-time">{{ clock }} / 3:00</span>
      <span v-if="busy" class="spinner" aria-hidden="true"></span>
      <button v-if="phase === 'unknown' && !canResend" class="button button-outline button-small"
        type="button" :disabled="disabled" @click="readStatus">读取转写状态</button>
      <button v-if="phase === 'unknown' && canResend" class="button button-outline button-small"
        type="button" :disabled="disabled" @click="upload">上传保留的录音</button>
      <button v-if="phase === 'failed'" class="button button-outline button-small"
        type="button" :disabled="disabled" @click="retryAudio">用原录音重试</button>
      <button v-if="phase !== 'idle' && phase !== 'review'" class="text-action" type="button"
        @click="reset()">{{ phase === 'recording' ? '取消录音' : '取消并继续打字' }}</button>
      <span v-if="phase === 'idle' && !notice">{{ !supported ? '当前页面无法录音，可继续打字。'
        : configured === undefined ? '正在确认语音功能是否可用。'
        : configured ? '停止录音后将发送到配置的百炼语音服务。' : '语音转写未启用，可继续打字。' }}</span>
      <span v-if="notice" role="status">{{ notice }}</span>
    </div>
    <div v-if="phase === 'review'" class="voice-draft">
      <strong>转写草稿</strong>
      <p>{{ candidate }}</p>
      <div class="voice-controls">
        <button class="button button-primary button-small" type="button"
          :disabled="disabled || answerWithCandidate.length > 8_000" @click="useCandidate">
          {{ answer.trim() ? '追加到回答' : '使用这段转写' }}
        </button>
        <button class="text-action" type="button" @click="reset()">丢弃此段</button>
        <span v-if="answerWithCandidate.length > 8_000" role="status">合并后超过 8,000 字，请先缩短已有回答。</span>
      </div>
    </div>
  </section>
</template>
