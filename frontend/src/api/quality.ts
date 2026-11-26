import { request } from './client'
import type { EntryRow } from '@/data/types'

export type QualityRun = {
  id?: number
  规则运行号: string
  回执编号?: string
  实体编码: string
  轮次: string
  检查总数: number
  命中数: number
  status?: string
  推送状态?: string
  有效确认次数: number
  confirmed?: boolean
  deliveries?: number
  同轮依据?: string
  [key: string]: unknown
}

export type FlowMessage = {
  ok: boolean
  message?: string
  run_no?: string
  report_id?: number
  report_no?: string
  duplicate?: boolean
  waiting?: boolean
  stat_id?: number
  [key: string]: unknown
}

async function postJson<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return (await response.json()) as T
}

export function registerReceipt(body: Record<string, unknown>): Promise<FlowMessage> {
  return postJson<FlowMessage>('/api/quality_rule/receipts', body)
}

export function interruptPush(): Promise<FlowMessage> {
  return postJson<FlowMessage>('/api/quality_rule/push/interrupt', {})
}

export function restorePush(runNo = ''): Promise<FlowMessage> {
  return postJson<FlowMessage>('/api/quality_rule/push/restore', runNo ? { run_no: runNo } : {})
}

export function retryRun(runNo: string): Promise<FlowMessage> {
  return postJson<FlowMessage>(`/api/quality_rule/runs/${encodeURIComponent(runNo)}/retry`, {})
}

export async function listRuns(): Promise<QualityRun[]> {
  const response = await request('/api/quality_rule/runs')
  if (!response.ok) return []
  const payload = (await response.json()) as { items?: QualityRun[] }
  return payload.items ?? []
}

export function continueReport(id: number, runNo = ''): Promise<FlowMessage & { entry?: EntryRow }> {
  return postJson(`/api/quality_result/${id}/continue`, runNo ? { run_no: runNo } : {})
}

export function bootstrapScenario(): Promise<FlowMessage> {
  return postJson<FlowMessage>('/api/quality_flow/scenario', {})
}

export type QualityBasis = {
  metrics: EntryRow[]
  empty_basis: EntryRow[]
  stats: EntryRow[]
  push_interrupted: boolean
}

export async function loadQualityBasis(): Promise<QualityBasis | null> {
  try {
    const response = await request('/api/stats_overview/quality-basis')
    if (!response.ok) return null
    return (await response.json()) as QualityBasis
  } catch {
    return null
  }
}

export function reportDownloadUrl(id: number, runNo = '', format = ''): string {
  const params = new URLSearchParams()
  if (runNo) params.set('run_no', runNo)
  if (format) params.set('format', format)
  const query = params.toString()
  return `/api/quality_result/${id}/download${query ? `?${query}` : ''}`
}

/** 直接用浏览器锚点下载，确保 Playwright 能捕获 download 事件。 */
export function clickDownload(url: string, filename?: string): void {
  const anchor = document.createElement('a')
  anchor.href = url
  if (filename) anchor.download = filename
  anchor.rel = 'noopener'
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
}
