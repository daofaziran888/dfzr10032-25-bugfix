<template>
  <section class="page" data-module="truncation">
    <header class="page-head">
      <div>
        <h2>采集载荷截断明细与接入对账</h2>
        <p class="page-desc">
          校验分页响应的声明条数、消息结束边界与已收到事件；定位断条页内位置与条目丢失页，
          重采前缀按原页序关联去重，装运按已接入事件放行、结算按原页序核齐整页。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="loadDemo">载入题设演示场景</button>
        <button class="btn" type="button" @click="refreshAll">刷新视图</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">通道</span>
        <strong class="stat-value">{{ channelCode }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已可放行运单</span>
        <strong class="stat-value">{{ releasableKeys.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">等待页尾位置</span>
        <strong class="stat-value">{{ pendingCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">整页状态</span>
        <strong class="stat-value" :class="{ 'error-text': !pageComplete }">
          {{ pageComplete ? '已核齐' : '未核齐' }}
        </strong>
      </article>
    </div>

    <div class="filter-bar">
      <label class="filter-item">
        <span>通道编号</span>
        <input v-model="channelCode" placeholder="如 SHIP-CH-01" />
      </label>
      <label class="filter-item">
        <span>页码</span>
        <input v-model.number="pageNo" type="number" min="1" />
      </label>
      <label class="filter-item">
        <span>稳定键字段</span>
        <input v-model="keyField" placeholder="waybill_no" />
      </label>
      <button class="btn" type="button" @click="refreshAll">查询</button>
    </div>

    <div class="grid-2">
      <article class="panel">
        <h3>① 登记采集 / 重试响应</h3>
        <form class="modal-body" @submit.prevent="submitPayload(false)">
          <label class="modal-field">
            <span>响应原文（允许截断，断在字段中间也可）</span>
            <textarea v-model="rawText" rows="8" placeholder='{"total":50,"declared_count":50,"events":[...]}'></textarea>
          </label>
          <div class="filter-bar">
            <label class="filter-item">
              <span>声明条数</span>
              <input v-model.number="declaredCount" type="number" />
            </label>
            <label class="filter-item">
              <span>登记总数</span>
              <input v-model.number="declaredTotal" type="number" />
            </label>
            <label class="filter-item">
              <span>采集轮次</span>
              <input v-model.number="attempt" type="number" min="1" />
            </label>
          </div>
          <footer class="modal-foot">
            <button class="btn primary" type="submit">登记首次采集</button>
            <button class="btn" type="button" @click="submitPayload(true)">登记重试响应</button>
          </footer>
        </form>
        <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
      </article>

      <article class="panel">
        <h3>② 截断明细（最近一次登记）</h3>
        <div v-if="lastDetail">
          <p>
            页状态：<strong :class="lastDetail.status === 'complete' ? 'ok-text' : 'error-text'">
              {{ statusLabel(lastDetail.status) }}
            </strong>
            · 声明 {{ lastDetail.declared_count ?? '—' }} 条 · 完整收到
            {{ lastDetail.received_complete }} 条 · 结束边界
            {{ lastDetail.end_boundary_present ? '完整' : '缺失' }}
          </p>
          <p v-if="lastDetail.partial_entry" class="error-text">
            断条位置：第 {{ lastDetail.partial_entry.position }} 条
            <template v-if="lastDetail.partial_entry.expected_key_hint">
              （残片键提示 {{ lastDetail.partial_entry.expected_key_hint }}）
            </template>
            ，字符偏移 {{ lastDetail.partial_entry.char_offset }}
          </p>
          <p v-if="lastDetail.missing_positions?.length" class="error-text">
            条目丢失位置：{{ lastDetail.missing_positions.join('、') }}
          </p>
          <ul class="issue-list">
            <li v-for="(issue, idx) in lastDetail.issues" :key="idx" :class="issueClass(issue.code)">
              [{{ issue.code }}] {{ issue.message }}
            </li>
          </ul>
        </div>
        <p v-else class="empty-state">尚未登记响应，可先载入演示场景。</p>
      </article>
    </div>

    <div class="grid-2">
      <article class="panel">
        <h3>③ 装运放行视图（重试回来前可查，每键一条）</h3>
        <table class="data-table">
          <thead>
            <tr><th>运单键</th><th>原页</th><th>页内位置</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in releasableRows" :key="row.key">
              <td>{{ row.key }}</td>
              <td>第 {{ row.page_no }} 页</td>
              <td>{{ row.position }}</td>
              <td class="ok-text">{{ row.status }}</td>
            </tr>
            <tr v-if="!releasableRows.length">
              <td colspan="4" class="empty-state">暂无可放行运单</td>
            </tr>
          </tbody>
        </table>
        <div v-for="p in pendingPages" :key="p.page_no" class="pending-box">
          第 {{ p.page_no }} 页：断条位置 {{ p.partial_position ?? '无' }}
          ，丢失位置 {{ p.missing_positions.length ? p.missing_positions.join('、') : '无' }}
        </div>
      </article>

      <article class="panel">
        <h3>④ 结算对账视图（按原页序 + 键值核齐整页）</h3>
        <div v-if="report">
          <p>
            整页 {{ report.page_complete ? '已核齐' : '未核齐' }}
            · 交付 {{ report.delivered_count }} 条 · 唯一键 {{ report.unique_key_count }} 个
            · 最新总数 {{ report.declared_total_latest ?? '—' }}
          </p>
          <p v-if="report.gaps?.length" class="error-text">缺口位置：{{ report.gaps.join('、') }}</p>
          <table class="data-table compact">
            <thead>
              <tr><th>原页序</th><th>运单键</th><th>目的地</th></tr>
            </thead>
            <tbody>
              <tr v-for="entry in report.ordered_entries" :key="entry.key">
                <td>{{ entry.position }}</td>
                <td>{{ entry.key }}</td>
                <td>{{ entry.data.destination ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="empty-state">该页暂无接入数据。</p>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

interface Issue { code: string; message: string }
interface Detail {
  status: string
  declared_count: number | null
  received_complete: number
  end_boundary_present: boolean
  missing_positions: number[]
  partial_entry: { position: number; char_offset: number; expected_key_hint: string | null } | null
  issues: Issue[]
}
interface ReleasableRow { key: string; page_no: number; position: number; status: string }
interface PendingPage { page_no: number; partial_position: number | null; missing_positions: number[] }
interface ReconcileReport {
  page_complete: boolean
  delivered_count: number
  unique_key_count: number
  declared_total_latest: number | null
  gaps: number[]
  ordered_entries: { position: number; key: string; data: Record<string, unknown> }[]
}

const channelCode = ref('SHIP-CH-01')
const pageNo = ref(1)
const keyField = ref('waybill_no')
const rawText = ref('')
const declaredCount = ref<number | null>(50)
const declaredTotal = ref<number | null>(50)
const attempt = ref(1)
const errorMessage = ref('')
const lastDetail = ref<Detail | null>(null)
const releasableRows = ref<ReleasableRow[]>([])
const pendingPages = ref<PendingPage[]>([])
const report = ref<ReconcileReport | null>(null)

const releasableKeys = computed(() => releasableRows.value.map((r) => r.key))
const pendingCount = computed(() =>
  pendingPages.value.reduce(
    (sum, p) => sum + (p.partial_position ? 1 : 0) + p.missing_positions.length,
    0,
  ),
)
const pageComplete = computed(() => report.value?.page_complete ?? false)

function statusLabel(status: string) {
  return { complete: '完整', truncated: '截断', malformed: '结构异常' }[status] ?? status
}

function issueClass(code: string) {
  return code === 'total_drift' ? 'warn-text' : 'error-text'
}

async function postJson(path: string, body: unknown) {
  const resp = await request(path, { method: 'POST', body: JSON.stringify(body) })
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}))
    throw new Error(data.detail || `接口返回 ${resp.status}`)
  }
  return resp.json()
}

async function submitPayload(isRetry: boolean) {
  errorMessage.value = ''
  const path = `/api/truncation/channels/${encodeURIComponent(channelCode.value)}/${isRetry ? 'retries' : 'payloads'}`
  try {
    lastDetail.value = await postJson(path, {
      page_no: pageNo.value,
      raw_text: rawText.value,
      key_field: keyField.value,
      attempt: attempt.value,
      request_declared_count: declaredCount.value,
      request_declared_total: declaredTotal.value,
    })
    await refreshAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  }
}

async function refreshAll() {
  errorMessage.value = ''
  const code = encodeURIComponent(channelCode.value)
  try {
    const [rel, rec] = await Promise.all([
      (await request(`/api/truncation/channels/${code}/releasable`)).json(),
      request(`/api/truncation/channels/${code}/pages/${pageNo.value}/reconcile`)
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null),
    ])
    releasableRows.value = rel.releasable ?? []
    pendingPages.value = rel.pending_pages ?? []
    report.value = rec
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '视图刷新失败'
  }
}

// ---- 题设演示：50 条声明、第 37 条字段中间断开；重试总数 52、前 20 条重发 ----
function event(seq: number, total: number) {
  return {
    waybill_no: `WB${String(seq).padStart(4, '0')}`,
    seq,
    origin: '上海仓',
    destination: '北京站',
    total_snapshot: total,
  }
}

function envelope(events: unknown[], total: number, count: number) {
  return JSON.stringify({ code: 0, total, declared_count: count, events })
}

async function loadDemo() {
  errorMessage.value = ''
  channelCode.value = 'SHIP-DEMO'
  pageNo.value = 1
  keyField.value = 'waybill_no'
  declaredCount.value = 50
  declaredTotal.value = 50
  attempt.value = 1

  // 首次响应：前 36 条完整，第 37 条断在 destination 字段中间
  const firstEvents = Array.from({ length: 36 }, (_, i) => event(i + 1, 50))
  const firstHead = envelope(firstEvents, 50, 50)
  const fragment = '{"waybill_no": "WB0037", "seq": 37, "origin": "上海仓", "destination": "北京'
  rawText.value = `${firstHead.slice(0, firstHead.lastIndexOf(']'))}, ${fragment}`
  await submitPayload(false)

  // 第一次重试：总数更正为 52，前 20 条重发，补到第 40 条
  const retry1 = [
    ...Array.from({ length: 20 }, (_, i) => ({ ...event(i + 1, 52), _position: i + 1 })),
    ...Array.from({ length: 4 }, (_, i) => ({ ...event(i + 37, 52), _position: i + 37 })),
  ]
  rawText.value = envelope(retry1, 52, retry1.length)
  declaredCount.value = 52
  attempt.value = 2
  await submitPayload(true)

  // 第二次重试：补齐 41..52，整页核齐
  const retry2 = Array.from({ length: 52 }, (_, i) => ({ ...event(i + 1, 52), _position: i + 1 }))
  rawText.value = envelope(retry2, 52, 52)
  attempt.value = 3
  await submitPayload(true)
}

onMounted(refreshAll)
</script>

<style scoped>
.grid-2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 16px;
  margin: 16px 0;
}
.panel {
  background: var(--card-bg, #fff);
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 8px;
  padding: 16px;
}
.panel h3 {
  margin: 0 0 12px;
  font-size: 15px;
}
textarea {
  width: 100%;
  font-family: ui-monospace, monospace;
  font-size: 12px;
}
.issue-list {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 13px;
}
.issue-list li {
  margin-bottom: 4px;
}
.warn-text {
  color: #b8860b;
}
.ok-text {
  color: #1a7f37;
}
.pending-box {
  margin-top: 8px;
  padding: 8px 12px;
  background: #fff7e6;
  border-radius: 6px;
  font-size: 13px;
}
.data-table.compact {
  max-height: 320px;
  overflow-y: auto;
  display: block;
}
</style>
