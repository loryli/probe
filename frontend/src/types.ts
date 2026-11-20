export type SessionStatus = 'active' | 'paused' | 'completed'
export type TurnRole = 'interviewer' | 'candidate'
export type InterviewStage = 'self_introduction' | 'project_introduction' | 'technical'

export interface TurnDecision {
  objective_status: 'covered' | 'partial' | 'insufficient'
  action: 'follow_up' | 'continue' | 'switch' | 'complete'
  observation: string
  reason: string
  followups_used: number
}

export interface InterviewTurn {
  stage: InterviewStage
  id: string
  sequence: number
  role: TurnRole
  objective_id: string | null
  source: 'project' | 'question_bank' | null
  question_id: string | null
  is_followup: boolean
  text: string
  answered: boolean
  skipped: boolean
  decision: TurnDecision | null
  created_at: string
}

export interface ObjectiveState {
  id: string
  topic: string
  source: 'project' | 'question_bank'
  priority: 'high' | 'medium' | 'low'
  status: 'pending' | 'active' | 'covered' | 'skipped'
  asked: number
  followups: number
  max_followups: number
  time_budget_sec: number
  elapsed_sec: number
}

export interface InterviewSession {
  phase: InterviewStage
  practice_mode: 'question_bank' | 'resume'
  id: string
  plan_id: string
  plan_revision: number
  status: SessionStatus
  revision: number
  mode: 'model' | 'rule'
  provider: string
  model: string
  direction: string
  duration_target_sec: number
  elapsed_sec: number
  remaining_sec: number
  objectives: ObjectiveState[]
  turns: InterviewTurn[]
  current_turn: InterviewTurn | null
  pending_turn_id: string | null
  answer_count: number
  question_count: number
  followups_used: number
  completion_reason: string
  error_code: string | null
  error_message: string | null
  started_at: string
  ended_at: string | null
  created_at: string
  updated_at: string
}

export interface SessionSummary {
  id: string
  plan_id: string
  status: SessionStatus
  mode: 'model' | 'rule'
  direction: string
  answer_count: number
  question_count: number
  objective_total: number
  objective_done: number
  elapsed_sec: number
  created_at: string
  started_at: string
  ended_at: string | null
}

export interface SessionOptions {
  plans: Array<{
    practice_mode: 'question_bank' | 'resume'
    id: string
    direction: string
    duration_minutes: number
    objective_count: number
    usable: boolean
    reason: string
    confirmed_at: string | null
    created_at: string
  }>
  model: Record<string, unknown>
}

export interface SessionReport {
  id: string
  session_id: string
  status: 'ready' | 'failed'
  source: 'model' | 'rule'
  report: {
    overall: 'solid' | 'mixed' | 'weak'
    summary: string
    evaluations: Array<{
      objective_id: string
      status: 'covered' | 'partial' | 'missed'
      comment: string
      highlights: string[]
      gaps: string[]
      turn_sequences: number[]
    }>
    weak_topics: string[]
    suggestions: string[]
    risks: string[]
  } | null
  stats: Record<string, number>
  error_code: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface TurnAnalysisEvidence {
  turn_id: string
  quote: string
}

export interface MemoryCandidate {
  id: string
  source_observation_id: string
  turn_id: string
  topic_key: string
  observation_type: string
  statement: string
  direction: 'supports' | 'contradicts' | 'unknown'
  confidence: number
  evidence: TurnAnalysisEvidence[]
  status: 'pending' | 'confirmed' | 'rejected'
  revision: number
  observed_at: string
  decided_at: string | null
  created_at: string
  updated_at: string
}

export interface TurnAnalysisJob {
  id: string
  session_id: string
  turn_id: string
  status: 'pending' | 'running' | 'ready' | 'failed'
  attempt_count: number
  retryable: boolean
  result: {
    schema_version: 'turn_analysis.v1'
    turn_id: string
    knowledge_points: Array<{
      topic: string
      coverage: 'covered' | 'partial' | 'missing' | 'uncertain'
    }>
    observations: Array<{
      type: 'strength' | 'missing_detail' | 'incorrect' | 'unverified_claim' | 'communication'
      topic: string
      statement: string
      confidence: number
      evidence: TurnAnalysisEvidence[]
    }>
    technical_accuracy: {
      label: 'accurate' | 'plausible_incomplete' | 'incorrect' | 'uncertain'
      reason: string
      evidence: TurnAnalysisEvidence[]
    }
    followup_suggestions: string[]
    warnings: string[]
  } | null
  observations: Array<{
    id: string
    topic: string
    observation_type: string
    statement: string
    confidence: number
    evidence: TurnAnalysisEvidence[]
    created_at: string
  }>
  memory_candidates: MemoryCandidate[]
  elapsed_ms: number | null
  input_tokens: number | null
  output_tokens: number | null
  error_code: string | null
  error_message: string | null
  created_at: string
  updated_at: string
  completed_at: string | null
}

export interface TurnAnalysisPage {
  items: TurnAnalysisJob[]
}

export interface SessionPage {
  items: SessionSummary[]
  total: number
  limit: number
  offset: number
}
