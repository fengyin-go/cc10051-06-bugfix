<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <!-- 质量报告：中断演练 / 恢复重试 入口 -->
    <div v-if="isQualityResult" class="quality-flow-bar" data-testid="quality-flow-bar">
      <button class="btn" data-testid="prepare-scenario" type="button" @click="prepareScenario">
        一键布置「空报告 + 10检1中迟到回执 + 推送中断」
      </button>
      <button class="btn" data-testid="restore-push" type="button" @click="restoreGlobal">
        恢复回执并重试一次
      </button>
      <span v-if="flowMessage" class="flow-message" data-testid="quality-flow-message">
        {{ flowMessage }}
      </span>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <!-- 质量规则库：迟到回执登记 + 运行账目 -->
    <section v-if="isQualityRule" class="quality-panel" data-testid="rule-receipt-panel">
      <h3>迟到检查回执 / 同轮依据 / 推送演练</h3>
      <form class="receipt-form" data-testid="late-receipt-form" @submit.prevent="submitReceipt">
        <label class="create-field">
          <span>实体编码</span>
          <input v-model="receiptForm.entity" data-testid="receipt-entity" placeholder="与报告同一实体" />
        </label>
        <label class="create-field">
          <span>轮次</span>
          <input v-model="receiptForm.round" data-testid="receipt-round" placeholder="与报告同一轮" />
        </label>
        <label class="create-field">
          <span>规则运行号（可留空自动生成）</span>
          <input v-model="receiptForm.run_no" data-testid="receipt-run-no" />
        </label>
        <label class="create-field">
          <span>回执编号（幂等键）</span>
          <input v-model="receiptForm.receipt_no" data-testid="receipt-no" />
        </label>
        <label class="create-field narrow">
          <span>检查总数</span>
          <input v-model.number="receiptForm.total" data-testid="receipt-total" type="number" min="0" />
        </label>
        <label class="create-field narrow">
          <span>命中数</span>
          <input v-model.number="receiptForm.hits" data-testid="receipt-hits" type="number" min="0" />
        </label>
        <label class="create-field check-field">
          <input v-model="receiptForm.interrupt" data-testid="receipt-interrupt" type="checkbox" />
          <span>送达时中断推送</span>
        </label>
        <div class="page-actions">
          <button class="btn primary" data-testid="submit-receipt" type="submit">登记迟到回执</button>
          <button class="btn" data-testid="interrupt-push" type="button" @click="interruptPush">中断推送</button>
          <button class="btn" data-testid="restore-push-rule" type="button" @click="restoreGlobal">恢复并重试</button>
        </div>
      </form>
      <p v-if="ruleMessage" class="flow-message" data-testid="rule-flow-message">{{ ruleMessage }}</p>

      <h4>检查运行账目（推送中断也可独立查询，凭规则运行号续办）</h4>
      <table class="data-table run-ledger" data-testid="run-ledger">
        <thead>
          <tr>
            <th>规则运行号</th><th>回执编号</th><th>实体编码</th><th>轮次</th>
            <th>检查/命中</th><th>推送状态</th><th>有效确认</th><th>送达次数</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="run in runs" :key="run.规则运行号" :data-testid="`run-row-${run.规则运行号}`">
            <td :data-testid="`ledger-run-no-${run.规则运行号}`">{{ run.规则运行号 }}</td>
            <td>{{ run.回执编号 }}</td>
            <td>{{ run.实体编码 }}</td>
            <td>{{ run.轮次 }}</td>
            <td>{{ run.检查总数 }} / {{ run.命中数 }}</td>
            <td :data-testid="`run-push-state-${run.规则运行号}`">{{ run.推送状态 }}</td>
            <td :data-testid="`run-confirm-count-${run.规则运行号}`">{{ run.有效确认次数 }}</td>
            <td>{{ run.deliveries ?? 1 }}</td>
            <td>
              <button
                class="link"
                type="button"
                :data-testid="`retry-run-${run.规则运行号}`"
                @click="retryOne(run.规则运行号)"
              >恢复并重试</button>
            </td>
          </tr>
          <tr v-if="!runs.length"><td colspan="9" class="empty-state">暂无检查运行账目</td></tr>
        </tbody>
      </table>
    </section>

    <!-- 治理看板：同批计分理由依据 -->
    <section v-if="isStats" class="quality-panel" data-testid="quality-basis">
      <h3>质量指标当前依据（仅实际检查汇入后才形成指标）</h3>
      <p v-if="basis?.push_interrupted" class="warn-text" data-testid="basis-push-interrupted">
        当前规则库 → 报告推送处于中断状态；中断期间不会产生新的看板质量指标。
      </p>
      <div data-testid="basis-metrics">
        <article
          v-for="item in basis?.metrics ?? []"
          :key="String(item.report_id ?? item.id)"
          class="basis-card"
          :data-testid="`basis-${item.report_id ?? item.id}`"
        >
          <header>
            <strong :data-testid="`basis-report-no-${item.report_id ?? item.id}`">
              {{ item.报告编号 }}
            </strong>
            <span>实体 {{ item.实体编码 }} · 轮次 {{ item.轮次 }} · 运行号 {{ item.规则运行号 || '—' }}</span>
            <b class="score-badge" :data-testid="`basis-score-${item.report_id ?? item.id}`">{{ item.评分结果 }}</b>
          </header>
          <p :data-testid="`basis-reason-${item.report_id ?? item.id}`" class="basis-reason">
            计分理由：{{ item.计分理由 }}
          </p>
        </article>
        <p v-if="!(basis?.metrics ?? []).length" class="empty-state">
          暂无已汇入实际检查的质量指标；空报告不会把空结果算成合格。
        </p>
      </div>

      <h4>空态报告依据（不计合格、不发质量指标，原报告仍可定位）</h4>
      <div class="basis-empty-list" data-testid="basis-empty">
        <span
          v-for="item in basis?.empty_basis ?? []"
          :key="String(item.report_id)"
          class="basis-empty-chip"
          :data-testid="`basis-empty-${item.report_id}`"
        >
          {{ item.报告编号 }}（{{ item.实体编码 }} / 轮次 {{ item.轮次 }}）：{{ item.可用状态 || '空态·待规则回执' }}
        </span>
      </div>

      <h4>看板质量指标快照</h4>
      <ul class="basis-stat-list" data-testid="basis-stats">
        <li v-for="s in basis?.stats ?? []" :key="String(s.id)" :data-testid="`basis-stat-${s.id}`">
          快照#{{ s.id }} · {{ s.报告编号 }} · {{ s.质量评分 }} ·
          <span :data-testid="`basis-stat-reason-${s.id}`">{{ s.计分理由 }}</span>
        </li>
      </ul>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="row in rows" :key="String(row.id)">
          <tr :data-testid="`row-${row.id}`" :data-status="String(row.status)">
            <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
            <td>
              {{ row.status }}
              <span
                v-if="isQualityResult && row['可用状态']"
                class="availability-tag"
                :data-code="String(row.availability_code ?? '')"
                :data-testid="`availability-tag-${row.id}`"
              >{{ row['可用状态'] }}</span>
            </td>
            <td class="row-actions">
              <button
                v-for="action in meta.actions"
                :key="action"
                class="link"
                type="button"
                :data-testid="`action-${row.id}-${action}`"
                @click="runAction(action, row)"
              >
                <template v-if="isQualityResult && action === '下载报告'">
                  <span :data-testid="`download-${row.id}`">
                    <span :data-testid="`row-download-${row.id}`">
                      <span :data-testid="`download-report-${row.id}`">
                        <span :data-testid="`report-download-${row.id}`">下载报告</span>
                      </span>
                    </span>
                  </span>
                </template>
                <template v-else-if="isQualityResult && action === '续办'">
                  <span :data-testid="`continue-${row.id}`">
                    <span :data-testid="`resume-${row.id}`">
                      <span :data-testid="`handle-${row.id}`">续办</span>
                    </span>
                  </span>
                </template>
                <template v-else>{{ action }}</template>
              </button>
            </td>
          </tr>
          <tr v-if="isQualityResult" class="quality-detail-row" :data-testid="`quality-detail-${row.id}`">
            <td :colspan="meta.fields.length + 2">
              <div class="qdetail-grid">
                <span>报告编号：<b :data-testid="`report-no-${row.id}`">{{ row['报告编号'] || row['报告批次'] }}</b></span>
                <span>轮次：<b :data-testid="`round-${row.id}`">{{ row['轮次'] || 'R1' }}</b></span>
                <span>
                  服务端可用状态：
                  <b :data-testid="`availability-${row.id}`" :data-code="String(row.availability_code ?? '')">
                    <span :data-testid="`report-status-${row.id}`">{{ row['可用状态'] || row.status }}</span>
                  </b>
                </span>
                <span>
                  规则运行号：
                  <b :data-testid="`run-no-${row.id}`">{{ displayRunNo(row) }}</b>
                </span>
                <span>有效确认次数：<b :data-testid="`confirm-count-${row.id}`">{{ row['有效确认次数'] ?? 0 }}</b></span>
              </div>
              <div
                v-if="row['空态原因']"
                class="qdetail-reason"
                :data-testid="`empty-reason-${row.id}`"
              >空态原因：{{ row['空态原因'] }}</div>
              <div
                v-if="row['计分理由']"
                class="qdetail-rationale"
                :data-testid="`rationale-${row.id}`"
              >计分理由：{{ row['计分理由'] }}</div>
              <div class="qdetail-runinput">
                <label>
                  凭规则运行号续办：
                  <input
                    v-model="runInputs[Number(row.id)]"
                    :data-testid="`run-input-${row.id}`"
                    placeholder="填入 QRUN 运行号；留空自动匹配同实体同轮的唯一运行"
                  />
                </label>
              </div>
            </td>
          </tr>
        </template>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in createFields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div v-if="isQualityResult" class="create-field">
          <span>轮次（同一轮才能汇入检查）</span>
          <input v-model="createValues['轮次']" data-testid="create-轮次" placeholder="例如 R20261126，留空为 R1" />
        </div>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import {
  bootstrapScenario,
  clickDownload,
  continueReport,
  interruptPush as apiInterruptPush,
  listRuns,
  loadQualityBasis,
  registerReceipt,
  reportDownloadUrl,
  restorePush,
  retryRun,
  type QualityBasis,
  type QualityRun,
} from '@/api/quality'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const isQualityResult = computed(() => meta.key === 'quality_result')
const isQualityRule = computed(() => meta.key === 'quality_rule')
const isStats = computed(() => meta.key === 'stats_overview')

// 质量续办相关前端状态
const runInputs = ref<Record<number, string>>({})
const runs = ref<QualityRun[]>([])
const basis = ref<QualityBasis | null>(null)
const flowMessage = ref('')
const ruleMessage = ref('')
const receiptForm = ref({
  entity: 'QUAL-S11-001',
  round: 'R20261126',
  run_no: '',
  receipt_no: '',
  total: 10,
  hits: 1,
  interrupt: true,
})

const createFields = computed(() => meta.fields)

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function displayRunNo(row: EntryRow): string {
  const bound = String(row['规则运行号'] ?? '').trim()
  if (bound && bound !== 'null') return bound
  const candidates = row['候选规则运行号']
  if (Array.isArray(candidates) && candidates.length) {
    return candidates.join('，')
  }
  return '尚未取得（仍待规则回执）'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  createValues.value['轮次'] = 'R1'
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  flowMessage.value = ''
  try {
    if (isQualityResult.value && action === '下载报告') {
      await handleDownload(row)
      return
    }
    if (isQualityResult.value && action === '续办') {
      await handleContinue(row)
      return
    }
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function handleContinue(row: EntryRow) {
  const runNo = (runInputs.value[Number(row.id)] || String(row['规则运行号'] ?? '')).trim()
  const result = await continueReport(Number(row.id), runNo && runNo !== 'null' ? runNo : '')
  flowMessage.value = result.message || (result.ok ? '续办完成' : '续办未完成')
  if (!result.ok && !result.waiting) throw new Error(flowMessage.value)
  await reload()
}

async function handleDownload(row: EntryRow) {
  const id = Number(row.id)
  const typedRunNo = (runInputs.value[id] || '').trim()
  // 如果在输入框里给了规则运行号，先凭号续办（幂等），再下载与最新状态绑定的文件。
  if (typedRunNo) {
    const result = await continueReport(id, typedRunNo)
    flowMessage.value = result.message || ''
  }
  clickDownload(reportDownloadUrl(id))
  await reload()
}

async function prepareScenario() {
  errorMessage.value = ''
  try {
    const result = await bootstrapScenario()
    flowMessage.value = `${result.message || '演练场景已布置'}（报告#${result.report_id} ${result.report_no ?? ''}，运行号 ${result.run_no ?? ''}）`
    receiptForm.value.entity = String(result.entity ?? 'QUAL-S11-001')
    receiptForm.value.round = String(result.round ?? 'R20261126')
    if (result.run_no) receiptForm.value.run_no = String(result.run_no)
    await reload()
    await refreshRuns()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '场景布置失败'
  }
}

async function submitReceipt() {
  ruleMessage.value = ''
  try {
    const body: Record<string, unknown> = { ...receiptForm.value }
    const result = await registerReceipt(body)
    ruleMessage.value = result.message || '回执已登记'
    if (result.run_no) receiptForm.value.run_no = String(result.run_no)
    await refreshRuns()
    await reload()
  } catch (error) {
    ruleMessage.value = error instanceof Error ? error.message : '回执登记失败'
  }
}

async function interruptPush() {
  await apiInterruptPush()
  ruleMessage.value = '已中断推送'
  await refreshRuns()
  await loadBasis()
}

async function restoreGlobal() {
  const targetRun = isQualityRule.value ? '' : ''
  const result = await restorePush(targetRun)
  flowMessage.value = result.message || '推送已恢复并重试'
  ruleMessage.value = flowMessage.value
  await refreshRuns()
  await reload()
  await loadBasis()
}

async function retryOne(runNo: string) {
  const result = await retryRun(runNo)
  ruleMessage.value = result.message || '已重试'
  await refreshRuns()
  await reload()
}

async function refreshRuns() {
  if (!isQualityRule.value) return
  runs.value = await listRuns()
}

async function loadBasis() {
  if (!isStats.value) return
  basis.value = await loadQualityBasis()
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    await Promise.all([refreshRuns(), loadBasis()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.quality-flow-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: 8px 0 12px;
  padding: 10px 12px;
  border: 1px dashed #c084fc;
  border-radius: 8px;
  background: #faf5ff;
}
.flow-message {
  color: #6b21a8;
  font-size: 13px;
}
.quality-panel {
  margin: 12px 0;
  padding: 12px 14px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fcfcff;
}
.quality-panel h3 {
  margin: 0 0 10px;
  font-size: 15px;
}
.quality-panel h4 {
  margin: 14px 0 8px;
  font-size: 14px;
}
.receipt-form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
  align-items: end;
}
.receipt-form .create-field.narrow {
  max-width: 140px;
}
.check-field {
  display: flex;
  align-items: center;
  gap: 6px;
  padding-bottom: 8px;
}
.run-ledger {
  font-size: 13px;
}
.quality-detail-row > td {
  background: #f8fafc;
  padding: 10px 14px;
}
.qdetail-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 22px;
  font-size: 13px;
}
.availability-tag {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 8px;
  border-radius: 999px;
  background: #fef3c7;
  color: #92400e;
  font-size: 12px;
}
.availability-tag[data-code='ready'],
.availability-tag[data-code='historical_scored'] {
  background: #dcfce7;
  color: #166534;
}
.availability-tag[data-code='push_interrupted'] {
  background: #fee2e2;
  color: #991b1b;
}
.qdetail-reason {
  margin-top: 6px;
  color: #92400e;
  font-size: 13px;
}
.qdetail-rationale {
  margin-top: 6px;
  color: #166534;
  font-size: 13px;
}
.qdetail-runinput {
  margin-top: 8px;
  font-size: 13px;
}
.qdetail-runinput input {
  min-width: 320px;
  padding: 4px 8px;
}
.basis-card {
  border: 1px solid #bbf7d0;
  background: #f0fdf4;
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 8px;
}
.basis-card header {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  font-size: 13px;
}
.score-badge {
  color: #166534;
  font-size: 15px;
}
.basis-reason {
  margin: 6px 0 0;
  font-size: 13px;
}
.basis-empty-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.basis-empty-chip {
  background: #fef9c3;
  border: 1px solid #fde68a;
  color: #854d0e;
  border-radius: 999px;
  padding: 3px 10px;
  font-size: 12px;
}
.basis-stat-list {
  font-size: 13px;
}
.warn-text {
  color: #991b1b;
}
</style>
