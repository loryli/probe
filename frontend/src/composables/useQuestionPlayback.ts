import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

type PlaybackTarget = { key: string; text: string }

export function useQuestionPlayback(
  getTarget: () => PlaybackTarget | null,
  isMicrophoneActive: () => boolean,
) {
  const synth = window.speechSynthesis
  const supported = Boolean(synth && typeof window.SpeechSynthesisUtterance === 'function')
  const state = ref<'idle' | 'starting' | 'speaking'>('idle')
  const error = ref('')
  let current: SpeechSynthesisUtterance | null = null
  let startTimer: number | undefined

  function finish() {
    window.clearTimeout(startTimer)
    startTimer = undefined
    if (current) current.onstart = current.onend = current.onerror = null
    current = null
    state.value = 'idle'
  }

  function stop() {
    const hadUtterance = current !== null
    // 部分浏览器会在 cancel 时触发旧回调；先解除监听再停播。
    finish()
    if (hadUtterance) synth.cancel()
  }

  function toggle() {
    if (current) {
      stop()
      return
    }
    const target = getTarget()
    if (!supported || !target || isMicrophoneActive()) return

    error.value = ''
    const utterance = new SpeechSynthesisUtterance(target.text)
    const localVoices = synth.getVoices().filter((voice) => voice.localService
      && /^zh(?:[-_]|$)/i.test(voice.lang))
    const local = localVoices.find((voice) => /^zh[-_]CN$/i.test(voice.lang)) ?? localVoices[0]
    if (local) utterance.voice = local
    utterance.lang = local?.lang || 'zh-CN'
    current = utterance
    state.value = 'starting'
    utterance.onstart = () => {
      if (current !== utterance) return
      window.clearTimeout(startTimer)
      startTimer = undefined
      state.value = 'speaking'
    }
    utterance.onend = () => {
      if (current === utterance) finish()
    }
    utterance.onerror = (event) => {
      if (current !== utterance) return
      finish()
      error.value = event.error === 'language-unavailable' || event.error === 'voice-unavailable'
        ? '当前设备没有可用的中文朗读语音，可以继续看题作答。'
        : '朗读未完成，可以再试一次或继续看题。'
    }
    startTimer = window.setTimeout(() => {
      if (current !== utterance) return
      stop()
      error.value = '设备没有开始朗读，可以再试一次或继续看题。'
    }, 8_000)
    try {
      // 直接从点击触发，避免异步请求丢失浏览器需要的用户手势。
      synth.speak(utterance)
    } catch {
      stop()
      error.value = '设备暂时无法朗读，可以继续看题。'
    }
  }

  watch([() => getTarget()?.key, () => getTarget()?.text], () => {
    stop()
    error.value = ''
  }, { flush: 'sync' })
  watch(isMicrophoneActive, (active) => {
    if (active) stop()
  }, { flush: 'sync' })

  function stopIfHidden() {
    if (document.hidden) stop()
  }
  onMounted(() => {
    // 让浏览器提前加载语音列表；点朗读时再取最新结果。
    if (supported) synth.getVoices()
    window.addEventListener('pagehide', stop)
    document.addEventListener('visibilitychange', stopIfHidden)
  })
  onBeforeUnmount(() => {
    stop()
    window.removeEventListener('pagehide', stop)
    document.removeEventListener('visibilitychange', stopIfHidden)
  })

  return { supported, state, error, toggle }
}
