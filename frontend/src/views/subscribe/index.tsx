import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/subscribe'
const columns = [
  "任务编号",
  "数据源名称",
  "推送目标",
  "推送格式",
  "推送频率",
  "过滤条件",
  "最近推送",
  "任务状态"
]
const actions = [
  "启动推送",
  "暂停推送",
  "归档任务"
]

type Row = Record<string, string | number | null>
type OutputDetail = {
  可读输出: Array<Record<string, unknown>> | null
  输出字段: string[]
  空态说明: string
  快照状态?: string
  来源规则编号?: string
  可再次提交: boolean
}

export default function SubscribeView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<(OutputDetail & { 任务编号?: string }) | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('订阅任务列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据订阅推送列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '数据订阅推送动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据订阅推送操作失败')
    }
  }

  // 环节四：订阅详情只读取已确认的输出字段快照；未确认/无输出时展示空态与暂无数据说明。
  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/output`)
      if (!response.ok) {
        throw new Error('订阅详情读取失败')
      }
      const payload = (await response.json()) as OutputDetail & { 任务编号?: string }
      setDetail(payload)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅详情读取失败')
    }
  }

  return (
    <section className="page" data-module="subscribe">
      <header className="page-head">
        <div>
          <h2>数据订阅推送管理</h2>
          <p className="page-desc">维护数据订阅的推送目标、格式选择与频率控制；详情只读取加工规则已确认的输出字段快照，未确认的半成品不可见。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据订阅推送清单
          </button>
        </div>
      </header>

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
                <button className="link" type="button" onClick={() => void openDetail(row)}>
                  订阅详情
                </button>
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据订阅推送数据，可先登记订阅任务</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <article className="detail-panel">
          <header className="detail-head">
            <h3>订阅详情 · {detail.任务编号 ?? '输出读取'}</h3>
            <button className="btn ghost" type="button" onClick={() => setDetail(null)}>收起</button>
          </header>

          {!detail.可读输出 ? (
            // 没有可读输出：空态 + 暂无数据说明，不暴露任何半成品字段。
            <div className="empty-block">
              <p className="empty-state">{detail.空态说明 || '暂无数据'}</p>
              {detail.可再次提交 && (
                <p className="page-desc">转换任务受中断影响时可在数据加工转换页面再次提交，确认快照后此处即可读取。</p>
              )}
            </div>
          ) : (
            <div>
              <p className="page-desc">
                来源规则 {detail.来源规则编号} · 快照状态：{detail.快照状态} · 共 {detail.可读输出.length} 条输出
              </p>
              <table className="data-table">
                <thead>
                  <tr>
                    {detail.输出字段.map((field) => <th key={field}>{field}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {detail.可读输出.map((record, index) => (
                    <tr key={index}>
                      {detail.输出字段.map((field) => (
                        <td key={field}>{record[field] === null || record[field] === undefined ? '—' : String(record[field])}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </article>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据订阅推送记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
