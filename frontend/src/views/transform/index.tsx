import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/transform'
const columns = [
  "节点编号",
  "输入表名",
  "输出表名",
  "转换逻辑",
  "执行顺序",
  "依赖节点",
  "失败策略",
  "调度周期"
]
const actions = [
  "提交编排评审",
  "启用编排",
  "停用编排"
]
const statuses = [
  "草稿",
  "编排评审中",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "转换节点总数",
    "value": 0
  },
  {
    "label": "已启用节点",
    "value": 0
  },
  {
    "label": "编排评审中",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type Diagnostic = {
  代码?: string
  说明?: string
  节点?: string[]
  表?: string[]
}

type PlanStep = {
  执行序号: number
  节点编号: string
  输入表名: string
  输出表名: string
  执行顺序: number | null
  依赖节点: string
  本次动作: string
}

type LineageRow = {
  关系编号: string
  上游表名: string
  下游表名: string
  上游字段: string
  下游字段: string
  字段识别?: string
  原始输入?: string
  传递路径?: string[]
}

type EnableResult = {
  结论?: string
  版本号?: number
  错误?: Diagnostic[]
  执行计划预览?: PlanStep[]
  执行计划?: PlanStep[]
  血缘?: LineageRow[]
  归档血缘数?: number
  版本?: number[]
}

type FormState = Record<string, string>

const emptyForm: FormState = {
  节点编号: '',
  输入表名: '',
  输出表名: '',
  执行顺序: '',
  依赖节点: '',
  字段映射: '',
}

const formPlaceholders: Record<string, string> = {
  节点编号: '例如 TRAN_CLEAN',
  输入表名: '多个用逗号分隔，例如 DWD_ORDERS',
  输出表名: '例如 CLEAN_ORDERS',
  执行顺序: '整数，依赖节点必须更早，例如 30',
  依赖节点: '逗号分隔，例如 TRAN_LAND,TRAN_DWD',
  字段映射: '例如 DWD_ORDERS.amount->CLEAN_ORDERS.total_amount',
}

const suggestedNodes = ['TRAN_LAND', 'TRAN_CLEAN']

export default function TransformView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [form, setForm] = useState<FormState>(emptyForm)
  const [showForm, setShowForm] = useState(false)
  const [enableTargets, setEnableTargets] = useState(suggestedNodes.join(','))
  const [conclusion, setConclusion] = useState<EnableResult | null>(null)
  const [busy, setBusy] = useState(false)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const statCards = useMemo(() => {
    const total = rows.length
    const enabled = rows.filter((row) => row.status === '已启用').length
    const reviewing = rows.filter((row) => row.status === '编排评审中').length
    return stats.map((item, index) => ({
      ...item,
      value: [total, enabled, reviewing][index] ?? item.value,
    }))
  }, [rows])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('转换节点列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '转换编排列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('转换编排动作未生效，请稍后重试')
      }
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(payload.message ?? '转换编排动作未生效')
        return
      }
      if (payload.entry?.结论 === '停用成功') {
        setConclusion(payload.entry)
      }
      setNotice(payload.message ?? '')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '转换编排操作失败')
    }
  }

  async function submitEnable(nodes: string, preview: boolean) {
    setErrorMessage('')
    setNotice('')
    setBusy(true)
    try {
      if (preview) {
        const response = await request(
          `${ENDPOINT}/enable/preview?nodes=${encodeURIComponent(nodes)}`,
        )
        if (!response.ok) {
          throw new Error('启用预演失败，请检查节点编号')
        }
        const payload = await response.json()
        setConclusion({
          结论: payload.结论,
          错误: payload.错误,
          执行计划预览: payload.执行计划预览,
          血缘: payload.待生成血缘,
        })
        if (payload.错误?.length) {
          setErrorMessage(`预演发现 ${payload.错误.length} 项问题，修正后才能启用`)
        } else {
          setNotice('预演通过：可按执行计划启用，启用后将生成对应字段级血缘')
        }
        return
      }
      const response = await request(`${ENDPOINT}/enable`, {
        method: 'POST',
        headers: { 'Idempotency-Key': `enable-${nodes.replace(/,/g, '_')}` },
        body: JSON.stringify({ nodes: nodes.split(',') }),
      })
      if (!response.ok) {
        throw new Error('启用请求未送达，请稍后重试')
      }
      const payload = await response.json()
      setConclusion(payload.entry ?? null)
      if (!payload.ok) {
        setErrorMessage(payload.message ?? '启用被拒绝')
      } else {
        setNotice(payload.message ?? '')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '启用编排失败')
    } finally {
      setBusy(false)
    }
  }

  async function createNode() {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values: form }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(payload.message ?? '转换节点登记失败')
        return
      }
      setNotice(payload.message ?? '')
      setForm(emptyForm)
      setShowForm(false)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '转换节点登记失败')
    }
  }

  return (
    <section className="page" data-module="transform">
      <header className="page-head">
        <div>
          <h2>转换编排管理</h2>
          <p className="page-desc">编排ETL转换任务的执行顺序和依赖关系，管理节点间的数据流转。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowForm((prev) => !prev)}>
            {showForm ? '收起登记表单' : '登记转换节点'}
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出转换编排清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {statCards.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      {showForm && (
        <section className="panel">
          <h3 className="panel-title">登记转换节点</h3>
          <p className="panel-desc">
            依赖节点填写节点编号（如 TRAN_LAND）；字段映射支持「源表.源字段→目标表.目标字段」，
            多组用逗号分隔，例如 order_id:order_id,amount:total_amount。
          </p>
          <div className="form-grid">
            {Object.keys(emptyForm).map((field) => (
              <label key={field} className="filter-item">
                <span>{field}</span>
                <input
                  value={form[field] ?? ''}
                  placeholder={formPlaceholders[field] ?? ''}
                  onChange={(event) => setForm((prev) => ({ ...prev, [field]: event.target.value }))}
                />
              </label>
            ))}
          </div>
          <div className="panel-actions">
            <button className="btn primary" type="button" onClick={() => void createNode()}>提交登记</button>
            <button className="btn ghost" type="button" onClick={() => setForm(emptyForm)}>清空</button>
          </div>
        </section>
      )}

      <section className="panel">
        <h3 className="panel-title">启用编排：依赖准入与血缘投影</h3>
        <p className="panel-desc">
          提交下游节点即可自动扩展到依赖闭包；启用前校验输入输出表、依赖节点和执行顺序，
          出现环、缺失依赖或同表多个生产者时整组拒绝。
        </p>
        <div className="filter-bar">
          <label className="filter-item grow">
            <span>待启用节点（逗号分隔）</span>
            <input
              value={enableTargets}
              placeholder="例如 TRAN_CLEAN 或 TRAN_CLEAN,TRAN_DWD"
              onChange={(event) => setEnableTargets(event.target.value)}
            />
          </label>
          <button
            className="btn"
            type="button"
            disabled={busy}
            onClick={() => void submitEnable(enableTargets, true)}
          >
            预演准入
          </button>
          <button
            className="btn primary"
            type="button"
            disabled={busy}
            onClick={() => void submitEnable(enableTargets, false)}
          >
            启用并生成血缘
          </button>
        </div>
        {conclusion && (
          <div className="conclusion">
            <div className="conclusion-head">
              <strong>启用结论：{conclusion.结论 ?? '—'}</strong>
              {conclusion.版本号 ? <span className="tag">版本 v{conclusion.版本号}</span> : null}
              {typeof conclusion.归档血缘数 === 'number' ? (
                <span className="tag">归档血缘 {conclusion.归档血缘数} 条</span>
              ) : null}
            </div>
            {conclusion.错误 && conclusion.错误.length > 0 && (
              <ul className="diagnostics">
                {conclusion.错误.map((item, index) => (
                  <li key={`${item.代码}-${index}`}>
                    <span className="diag-code">{item.代码}</span>
                    {item.说明}
                    {item.节点 && item.节点.length > 0 && (
                      <em className="diag-scope">节点：{item.节点.join(' → ')}</em>
                    )}
                    {item.表 && item.表.length > 0 && <em className="diag-scope">表：{item.表.join('、')}</em>}
                  </li>
                ))}
              </ul>
            )}
            {(conclusion.执行计划 ?? conclusion.执行计划预览) && (
              <table className="data-table compact">
                <thead>
                  <tr><th>执行序号</th><th>节点编号</th><th>输入表名</th><th>输出表名</th><th>依赖节点</th><th>本次动作</th></tr>
                </thead>
                <tbody>
                  {(conclusion.执行计划 ?? conclusion.执行计划预览 ?? []).map((step) => (
                    <tr key={`${step.执行序号}-${step.节点编号}`}>
                      <td>{step.执行序号}</td>
                      <td>{step.节点编号}</td>
                      <td>{step.输入表名}</td>
                      <td>{step.输出表名}</td>
                      <td>{step.依赖节点 || '—'}</td>
                      <td>{step.本次动作}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {conclusion.血缘 && conclusion.血缘.length > 0 && (
              <table className="data-table compact">
                <thead>
                  <tr><th>关系编号</th><th>上游</th><th>下游</th><th>上游字段</th><th>下游字段</th><th>识别方式</th><th>传递路径</th></tr>
                </thead>
                <tbody>
                  {conclusion.血缘.map((row) => (
                    <tr key={row.关系编号}>
                      <td>{row.关系编号}</td>
                      <td>{row.上游表名}</td>
                      <td>{row.下游表名}</td>
                      <td>{row.上游字段}</td>
                      <td>{row.下游字段}</td>
                      <td>{row.字段识别 || '—'}</td>
                      <td>{row.传递路径?.[0] ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </section>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无转换编排数据，可先登记转换节点</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条转换编排记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
