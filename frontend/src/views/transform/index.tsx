import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/transform'
const columns = [
  "规则编号",
  "输入格式",
  "输出格式",
  "映射表达式",
  "字段过滤",
  "编码设置",
  "转换日志",
  "规则状态"
]
const actions = [
  "启用规则",
  "暂停规则",
  "关闭规则"
]
const statuses = [
  "草稿",
  "生效中",
  "已暂停",
  "已关闭"
]

type Row = Record<string, string | number | null>
type LogItem = {
  结果: '成功' | '失败'
  规则编号: string
  缺失字段?: string[]
  重试位置?: number
  摘要?: string
  时间: string
}
type TaskItem = {
  id: number
  规则编号: string
  阶段: string
  中断: boolean
  成功: boolean
  缺失字段: string[]
  重试位置: number
  输出快照状态: string | null
}

type ActionResponse = { ok: boolean; message: string; entry?: Record<string, unknown> | null }

export default function TransformView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [activeRow, setActiveRow] = useState<Row | null>(null)
  const [logs, setLogs] = useState<LogItem[]>([])
  const [tasks, setTasks] = useState<TaskItem[]>([])
  const [recordsText, setRecordsText] = useState('[\n  {}\n]')
  const [keepFieldsText, setKeepFieldsText] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('加工规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据加工转换列表读取失败')
    }
  }, [filters])

  const loadRuleDetail = useCallback(async (row: Row) => {
    setActiveRow(row)
    setNotice('')
    const id = Number(row.id)
    try {
      const [logsResp, tasksResp] = await Promise.all([
        request(`${ENDPOINT}/${id}/logs`),
        request(`${ENDPOINT}/${id}/tasks`),
      ])
      if (logsResp.ok) {
        const logsPayload = await logsResp.json()
        setLogs(logsPayload.items ?? [])
      }
      if (tasksResp.ok) {
        const tasksPayload = await tasksResp.json()
        setTasks(tasksPayload.items ?? [])
      }
    } catch {
      setErrorMessage('规则详情读取失败，请稍后重试')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = (await response.json()) as ActionResponse
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '数据加工转换动作未生效，请稍后重试')
      }
      setNotice(payload.message)
      await reload()
      if (activeRow?.id === row.id) {
        await loadRuleDetail({ ...row, ...(payload.entry as Row) })
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据加工转换操作失败')
    }
  }

  async function submitTask() {
    if (!activeRow) {
      return
    }
    setErrorMessage('')
    setNotice('')
    let records: unknown
    try {
      records = JSON.parse(recordsText)
    } catch {
      setErrorMessage('输入记录不是合法 JSON，请检查后重新提交')
      return
    }
    const keepFields = keepFieldsText
      .split(/[,，、;；]/)
      .map((field) => field.trim())
      .filter(Boolean)
    try {
      const response = await request(`${ENDPOINT}/${activeRow.id}/tasks`, {
        method: 'POST',
        body: JSON.stringify({ records, keep_fields: keepFields.length ? keepFields : null }),
      })
      const payload = (await response.json()) as ActionResponse & { entry?: TaskItem }
      // 业务拒绝（裁剪失败、中断）同样返回可读消息，任务可再次提交，不算网络错误。
      setNotice(payload.message)
      if (!payload.ok) {
        setErrorMessage(payload.message)
      }
      await reload()
      await loadRuleDetail(activeRow)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '转换任务提交失败，可再次提交')
    }
  }

  async function confirmSnapshot() {
    if (!activeRow) {
      return
    }
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${activeRow.id}/snapshot/confirm`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      const payload = (await response.json()) as ActionResponse
      setNotice(payload.message)
      if (!payload.ok) {
        setErrorMessage(payload.message)
      }
      await reload()
      await loadRuleDetail(activeRow)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '快照确认失败')
    }
  }

  return (
    <section className="page" data-module="transform">
      <header className="page-head">
        <div>
          <h2>数据加工转换管理</h2>
          <p className="page-desc">维护数据加工的格式映射、字段过滤与编码配置；转换链路按解析、字段裁剪、日志落库、订阅读取四环节执行。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据加工转换清单
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
            <tr key={String(row.id)} className={activeRow?.id === row.id ? 'row-active' : ''}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void loadRuleDetail(row)}>
                  转换链路
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据加工转换数据，可先登记加工规则</td>
            </tr>
          )}
        </tbody>
      </table>

      {activeRow && (
        <article className="detail-panel">
          <header className="detail-head">
            <h3>转换链路 · {String(activeRow['规则编号'])}</h3>
            <button className="btn ghost" type="button" onClick={() => setActiveRow(null)}>收起</button>
          </header>

          <div className="detail-grid">
            <label className="filter-item">
              <span>输入记录（JSON 对象或数组）</span>
              <textarea
                rows={6}
                value={recordsText}
                onChange={(event) => setRecordsText(event.target.value)}
              />
            </label>
            <label className="filter-item">
              <span>裁剪保留字段（逗号分隔，留空用规则的字段过滤）</span>
              <textarea
                rows={6}
                value={keepFieldsText}
                placeholder="orderId, customer, amount"
                onChange={(event) => setKeepFieldsText(event.target.value)}
              />
            </label>
          </div>
          <div className="row-actions">
            <button className="btn primary" type="button" onClick={() => void submitTask()}>提交转换任务</button>
            <button className="btn" type="button" onClick={() => void confirmSnapshot()}>确认输出字段快照</button>
          </div>
          <p className="page-desc">仅「生效中」规则允许字段裁剪；快照确认前数据订阅详情读不到半成品；任务被中断或拒绝后可再次提交。</p>

          <h4>转换任务</h4>
          {tasks.length === 0 ? (
            <p className="empty-state">暂无转换任务，提交后在此查看解析与裁剪结果</p>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>任务号</th><th>阶段</th><th>结果</th><th>缺失字段</th><th>重试位置</th><th>快照状态</th><th>中断</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
                  <tr key={task.id}>
                    <td>{task.id}</td>
                    <td>{task.阶段}</td>
                    <td>{task.成功 ? '成功' : '未成功'}</td>
                    <td>{task.缺失字段.length ? task.缺失字段.join('、') : '—'}</td>
                    <td>{task.重试位置}</td>
                    <td>{task.输出快照状态 ?? '—'}</td>
                    <td>{task.中断 ? '是（可再次提交）' : '否'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h4>历史转换日志（按原规则编号保留，成功/失败分账）</h4>
          {logs.length === 0 ? (
            <p className="empty-state">暂无转换日志</p>
          ) : (
            <ul className="log-list">
              {logs.map((log, index) => (
                <li key={`${log.时间}-${index}`} className={log.结果 === '成功' ? 'log-ok' : 'log-fail'}>
                  {log.结果 === '成功'
                    ? log.摘要
                    : `失败：规则编号 ${log.规则编号}，缺失字段 ${(log.缺失字段 ?? []).join('、') || '无'}，重试位置 ${log.重试位置 ?? 0}`}
                  <span className="log-time">{log.时间}</span>
                </li>
              ))}
            </ul>
          )}
        </article>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据加工转换记录{statuses.length ? `（状态：${statuses.join('、')}）` : ''}</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
