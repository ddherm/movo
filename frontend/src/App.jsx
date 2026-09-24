import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'

const phaseNames = {
  IDEA: '记录想法',
  CLARIFY: '澄清需求',
  RESEARCH: '调查路线',
  PLAN_REVIEW: '等待方案确认',
  BUILD: '制作原型',
  WAITING_DECISION: '等待你的决定',
  VERIFY: '验证作品',
  READY: '可以验收',
  FAILED: '需要处理',
  PAUSED: '已暂停',
}

const stageNames = ['想法', '澄清', '调研', '方案', '开发', '验证', '交付']
const stageByPhase = {
  IDEA: 0,
  CLARIFY: 1,
  RESEARCH: 2,
  PLAN_REVIEW: 3,
  BUILD: 4,
  WAITING_DECISION: 4,
  VERIFY: 5,
  READY: 6,
}

function Icon({ name, size = 20, strokeWidth = 1.8 }) {
  const shared = {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth,
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
    'aria-hidden': true,
  }

  const shapes = {
    spark: <><path d="m12 2 1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8L12 2Z" /><path d="m19 18 .7 2.3L22 21l-2.3.7L19 24l-.7-2.3L16 21l2.3-.7L19 18Z" /></>,
    mic: <><rect x="9" y="2" width="6" height="13" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v4m-4 0h8" /></>,
    image: <><rect x="3" y="3" width="18" height="18" rx="3" /><circle cx="8.5" cy="8.5" r="1.5" /><path d="m21 15-5-5L5 21" /></>,
    arrow: <><path d="M4 12h15m-6-6 6 6-6 6" /></>,
    send: <><path d="m22 2-7 20-4-9-9-4Z" /><path d="M22 2 11 13" /></>,
    check: <path d="m4 12 5 5L20 6" />,
    close: <path d="M5 5 19 19M19 5 5 19" />,
    alert: <><path d="M12 3 2 21h20L12 3Z" /><path d="M12 9v5m0 3h.01" /></>,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    external: <><path d="M13 5h6v6m0-6-9 9" /><path d="M19 14v5H5V5h5" /></>,
    upload: <><path d="M12 16V3m-5 5 5-5 5 5" /><path d="M4 16v4h16v-4" /></>,
    message: <path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l2-4.2A8.5 8.5 0 1 1 21 11.5Z" />,
    activity: <><path d="M3 12h4l3-7 4 14 3-7h4" /></>,
    home: <><path d="m3 10 9-7 9 7v10H3V10Z" /><path d="M9 20v-7h6v7" /></>,
    layers: <><path d="m12 2 9 5-9 5-9-5 9-5Zm-9 10 9 5 9-5M3 17l9 5 9-5" /></>,
    refresh: <><path d="M20 7V3l-3 3a9 9 0 1 0 3.2 9" /><path d="M20 3v5h-5" /></>,
    phone: <><path d="M7 3H5a2 2 0 0 0-2 2c0 8.8 7.2 16 16 16a2 2 0 0 0 2-2v-2l-5-2-2 2a15 15 0 0 1-7-7l2-2-2-5Z" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    chevron: <path d="m9 6 6 6-6 6" />,
    shield: <><path d="m12 2 8 4v6c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6l8-4Z" /><path d="m9 12 2 2 4-4" /></>,
  }

  return <svg {...shared}>{shapes[name] || shapes.spark}</svg>
}

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(date)
}

function asList(value) {
  if (!value) return []
  if (Array.isArray(value)) return value.filter(Boolean).map(String)
  return String(value).split(/\n+/).map((part) => part.trim()).filter(Boolean)
}

function safeLink(value) {
  if (!value) return undefined
  try {
    const url = new URL(value, window.location.origin)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : undefined
  } catch {
    return undefined
  }
}

async function api(path, options = {}) {
  const isForm = options.body instanceof FormData
  const response = await fetch(path, {
    cache: 'no-store',
    ...options,
    headers: isForm ? options.headers : {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  })
  const contentType = response.headers.get('content-type') || ''
  const payload = contentType.includes('application/json')
    ? await response.json()
    : await response.text()
  if (!response.ok) {
    const detail = payload && typeof payload === 'object'
      ? payload.detail || payload.error || payload.message
      : payload
    throw new Error(typeof detail === 'string' && detail ? detail : `请求失败（${response.status}）`)
  }
  return payload
}

function SectionHeading({ eyebrow, title, aside }) {
  return <div className="section-heading">
    <div><span className="eyebrow">{eyebrow}</span><h2>{title}</h2></div>
    {aside && <span className="section-aside">{aside}</span>}
  </div>
}

function TextItems({ value, empty = '暂未记录' }) {
  const items = asList(value)
  if (!items.length) return <p className="muted">{empty}</p>
  return <ul className="text-items">{items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul>
}

function PlanField({ label, value }) {
  if (!asList(value).length) return null
  return <div className="plan-field"><h4>{label}</h4><TextItems value={value} /></div>
}

function StatusBadge({ online, phase }) {
  if (online === false) return <span className="status-pill status-offline"><span className="status-dot" />主机离线</span>
  if (online === null) return <span className="status-pill"><span className="status-dot" />正在连接</span>
  return <span className={`status-pill ${phase === 'READY' ? 'status-ready' : ''}`}>
    <span className="status-dot" />{phase ? phaseNames[phase] || phase : '主机在线'}
  </span>
}

function ProjectTimeline({ phase }) {
  const active = stageByPhase[phase]
  if (active === undefined) return null
  return <div className="timeline" aria-label={`当前阶段：${phaseNames[phase]}`}>
    {stageNames.map((name, index) => <div className={`timeline-step ${index < active ? 'done' : ''} ${index === active ? 'active' : ''}`} key={name}>
      <div className="timeline-line"><span>{index < active ? <Icon name="check" size={13} strokeWidth={2.7} /> : String(index + 1).padStart(2, '0')}</span></div>
      <small>{name}</small>
    </div>)}
  </div>
}

function BuildProgress({ progress }) {
  if (!progress) return null
  const percent = Number(progress.percent)
  const known = typeof progress.percent === 'number' && Number.isFinite(percent)
  const completed = known ? Math.max(0, Math.min(100, percent)) : 0
  const completedStages = Number.isInteger(progress.stage_index) ? progress.stage_index : null
  const stageCount = Number.isInteger(progress.stage_count) ? progress.stage_count : null
  const stageLabel = progress.stage_label || '正在同步项目阶段'
  const step = progress.step || '等待下一条执行记录'
  return <div className="build-progress" aria-live="polite">
    <div className="build-progress-heading">
      <div><span className="eyebrow">整体流程</span><strong>{stageLabel}</strong></div>
      <span className="build-progress-value">{known ? `${Math.round(completed)}%` : '进行中'}</span>
    </div>
    <div className={`build-progress-track ${progress.indeterminate ? 'is-active' : ''}`} role="progressbar" aria-label="整体流程阶段进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow={known ? completed : undefined} aria-valuetext={known ? `已完成 ${completedStages ?? '当前'} 个流程阶段，当前步骤：${step}` : `当前步骤：${step}`}>
      <span className="build-progress-fill" style={{ width: `${completed}%` }} />
      {progress.indeterminate && <span className="build-progress-activity" style={{ left: `${completed}%` }} />}
    </div>
    <p className="build-progress-step"><span>当前步骤</span>{step}</p>
    <p className="build-progress-note">{completedStages !== null && stageCount !== null ? `已完成 ${completedStages}/${stageCount} 个流程阶段。` : '百分比按已完成的整体流程阶段计算。'}{progress.indeterminate ? '当前步骤仍在执行，阶段内进度无法精确估算。' : ''}</p>
  </div>
}

function FolderPicker({ folders, loading, error, onNavigate, onSelect, onClose }) {
  return <div className="modal-backdrop" role="presentation" onClick={onClose}>
    <div className="modal folder-modal" role="dialog" aria-modal="true" aria-labelledby="folder-title" onClick={(event) => event.stopPropagation()}>
      <button className="modal-close" type="button" aria-label="关闭文件夹选择" onClick={onClose}><Icon name="close" size={19} /></button>
      <span className="modal-icon"><Icon name="layers" size={23} /></span>
      <h2 id="folder-title">选择项目存放位置</h2>
      <p>浏览运行服务的电脑上的文件夹，选一个现有位置即可。开始新项目时会自动创建专用文件夹，无需提前新建。手机访问时，这里显示的是电脑的文件夹。</p>
      <div className="folder-current" title={folders?.path || ''}>{folders?.path || '正在读取默认位置…'}</div>
      <div className="folder-shortcuts">
        {(folders?.roots || []).map((root) => <button type="button" key={root.path} className="folder-shortcut" onClick={() => onNavigate(root.path)} disabled={loading || root.path === folders?.path}>{root.label || root.path}</button>)}
        {folders?.parent && <button type="button" className="folder-shortcut" onClick={() => onNavigate(folders.parent)} disabled={loading}>上一级</button>}
      </div>
      {error && <div className="call-warning" role="alert">{error}</div>}
      <div className="folder-list" aria-label="文件夹列表">
        {loading ? <div className="folder-empty">正在读取文件夹…</div> : !folders?.folders?.length ? <div className="folder-empty">这里没有可进入的子文件夹</div> : folders.folders.map((folder) => <button type="button" className="folder-row" key={folder.path} onClick={() => onNavigate(folder.path)}><Icon name="layers" size={18} /><span>{folder.name}</span><Icon name="chevron" size={17} /></button>)}
      </div>
      <button className="button button-primary full-button" type="button" onClick={() => onSelect(folders?.path)} disabled={!folders?.path || loading}>选用此位置</button>
    </div>
  </div>
}

function PlanCard({ plan, busy, offline, onApprove, canApprove = false }) {
  if (!plan) return null
  const approved = plan.status === 'approved' || plan.status === 'APPROVED'
  return <section className="surface plan-card" id="plan-card">
    <div className="card-topline"><span className="card-kicker"><Icon name="layers" size={17} />方案卡 · v{plan.spec_version || 1}</span><span className={`mini-tag ${approved ? 'mini-tag-success' : ''}`}>{approved ? '已确认' : canApprove ? '待你确认' : '仅供查看'}</span></div>
    <h3>{plan.goal || '首版作品方案'}</h3>
    {plan.audience && <p className="plan-audience">为 <strong>{plan.audience}</strong> 设计</p>}
    <div className="plan-grid">
      <PlanField label="核心体验" value={plan.flow} />
      <PlanField label="首版功能" value={plan.scope} />
      <PlanField label="暂不包含" value={plan.exclusions} />
      <PlanField label="手机验收标准" value={plan.acceptance} />
    </div>
    {(plan.route || plan.reasons) && <div className="route-box">
      <span className="eyebrow">推荐路线</span>
      <strong>{plan.route}</strong>
      <TextItems value={plan.reasons} empty="" />
    </div>}
    <div className="source-box">
      <Icon name="shield" size={18} />
      <div>
        <strong>资料范围</strong>
        <p>{plan.source_scope || '未配置联网检索时，方案仅基于当前项目资料。'}</p>
        {!!plan.sources?.length && <div className="sources">{plan.sources.map((source, index) => {
          const href = safeLink(source.url)
          return href
            ? <a key={`${source.url}-${index}`} href={href} target="_blank" rel="noopener noreferrer">{source.title || source.url}<Icon name="external" size={13} /></a>
            : <span key={index}>{source.title || '来源'}</span>
        })}</div>}
      </div>
    </div>
    {canApprove && <div className="card-actions">
      <p>确认后，搭档才会开始正式制作。</p>
      <button className="button button-primary" type="button" disabled={!!busy || offline} onClick={onApprove}>
        {busy === 'approve' ? '正在确认…' : '确认方案，开始制作'}<Icon name="arrow" size={18} />
      </button>
    </div>}
  </section>
}

function DecisionCard({ question, choice, setChoice, reason, setReason, busy, offline, onSubmit }) {
  if (!question) return null
  return <section className="surface decision-card" id="decision-card">
    <div className="card-topline"><span className="card-kicker"><Icon name="alert" size={18} />需要你的决定</span><span className="mini-tag mini-tag-warm">已暂停受影响工作</span></div>
    <h3>{question.question || '请选择接下来的方向'}</h3>
    {question.impact && <p className="decision-impact">{question.impact}</p>}
    <div className="choices" role="radiogroup" aria-label="决策选项">
      {(question.options || []).map((option) => <label className={`choice ${choice === option.id ? 'selected' : ''}`} key={option.id}>
        <input type="radio" name={`decision-${question.id}`} value={option.id} checked={choice === option.id} onChange={() => setChoice(option.id)} />
        <span className="choice-marker" />
        <span className="choice-content"><strong>{option.label}</strong>{option.id === question.recommended_option_id && <span className="recommended">搭档推荐</span>}{option.impact && <small>{option.impact}</small>}</span>
      </label>)}
    </div>
    <label className="field-label" htmlFor="decision-reason">补充原因 <span>可选</span></label>
    <textarea id="decision-reason" className="input small-input" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="告诉搭档你为什么这样选" rows="2" />
    <button className="button button-primary full-button" type="button" disabled={!choice || !!busy || offline} onClick={onSubmit}>
      {busy === 'decision' ? '正在提交…' : '提交决定并继续'}<Icon name="arrow" size={18} />
    </button>
    <p className="helper-note">同一问题只会记录一次有效决定。语音和文字回答以先成功提交的为准。</p>
  </section>
}

function StartView({ idea, setIdea, files, setFiles, folderName, setFolderName, parentDir, folderLoading, onChooseFolder, existingProject, onCancel, busy, offline, onStart, voice, onVoice }) {
  return <div className="start-view">
    <div className="start-hero">
      <div className="hero-emblem"><span className="soundbar"><i /><i /><i /><i /><i /></span></div>
      <span className="eyebrow">从一个念头开始</span>
      <h1>{existingProject ? '创建新项目' : '有个想法？'}<br /><em>我们边聊边做。</em></h1>
      <p>上传一张参考截图，或直接描述你想做什么。搭档会先和你厘清重点，再给出可确认的方案。</p>
    </div>
    <form className="surface start-form" onSubmit={onStart}>
      <label className="field-label" htmlFor="idea-input">你想做什么？ <span>可以稍后再补充</span></label>
      <textarea id="idea-input" className="input idea-input" value={idea} onChange={(event) => setIdea(event.target.value)} placeholder="例如：给学生用的学习计划页面，想保留截图里简洁的卡片操作…" rows="5" />
      <div className="project-folder-fields">
        <label className="field-label" htmlFor="folder-name">项目文件夹名称 <span>留空自动命名并创建</span></label>
        <input id="folder-name" className="input" value={folderName} onChange={(event) => setFolderName(event.target.value)} placeholder="例如：学习计划助手" maxLength={80} />
        <span className="field-label folder-parent-label">存放在运行服务的电脑</span>
        <div className="folder-selection"><span title={parentDir}>{parentDir || (folderLoading ? '正在读取默认位置…' : '尚未选择')}</span><button className="button button-secondary" type="button" onClick={onChooseFolder} disabled={offline || folderLoading}>浏览文件夹</button></div>
        <p className="helper-note">直接选一个已有文件夹；开始时会在其中自动创建项目子文件夹，不用先建。</p>
      </div>
      <label className="upload-zone" htmlFor="start-upload">
        <input id="start-upload" type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => setFiles(Array.from(event.target.files || []))} />
        <span className="upload-icon"><Icon name="image" size={21} /></span>
        <span><strong>添加参考截图</strong><small>从手机相册选择，可不上传</small></span>
        <Icon name="plus" size={20} />
      </label>
      {!!files.length && <div className="selected-files" aria-live="polite">{files.map((file, index) => <span key={`${file.name}-${index}`}><Icon name="image" size={14} />{file.name}</span>)}</div>}
      <button type="submit" className="button button-primary full-button" disabled={(!idea.trim() && !files.length) || !parentDir || !!busy || offline}>
        {busy === 'start' ? '正在创建项目…' : '开始讨论作品'}<Icon name="arrow" size={18} />
      </button>
      <div className="start-separator"><span>或者</span></div>
      <button type="button" className="button button-quiet full-button" onClick={() => onVoice()} disabled={!voice.enabled || !parentDir || offline || !!busy}>
        <Icon name="mic" size={18} />{voice.enabled ? '进入语音通话' : '语音通话暂未就绪'}
      </button>
      {!voice.enabled && <p className="helper-note centered">{voice.reason || '请先在运行服务的电脑上配置语音服务。文字讨论仍可使用。'}</p>}
      {existingProject && <button type="button" className="text-button start-cancel" onClick={onCancel} disabled={!!busy}>返回当前项目</button>}
    </form>
    <div className="start-promise"><Icon name="shield" size={18} /><span>方案确认前不会开始正式编码；项目资料保存在运行服务的电脑上。</span></div>
  </div>
}

function Conversation({ project, message, setMessage, busy, offline, onSend, voice, onVoice }) {
  const messages = project.messages || []
  return <section className="conversation-view">
    <div className="conversation-intro"><span className="eyebrow">同一个项目，同一段对话</span><h2>和搭档继续聊</h2><p>可以补充目标、询问进度或请搭档排查当前错误。回答会进入当前项目。</p></div>
    <div className="conversation-feed" aria-live="polite">
      {!messages.length && <div className="empty-conversation"><div className="empty-icon"><Icon name="message" size={24} /></div><strong>对话从这里开始</strong><p>说说作品要给谁用、最重要的操作是什么。</p></div>}
      {messages.map((item, index) => <div className={`message-row ${item.role === 'user' ? 'from-user' : 'from-agent'}`} key={item.id || index}>
        {item.role !== 'user' && <span className="avatar">伴</span>}
        <div className="message-bubble"><p>{item.text}</p><time>{formatTime(item.created_at)}</time></div>
      </div>)}
      {busy === 'diagnose' && <div className="message-row from-agent" role="status"><span className="avatar">伴</span><div className="message-bubble"><p>正在检查项目状态和错误记录…</p></div></div>}
    </div>
    <form className="composer" onSubmit={onSend}>
      <textarea className="input composer-input" aria-label="发送消息" value={message} onChange={(event) => setMessage(event.target.value)} placeholder="输入想法、询问进度或错误原因…" rows="2" onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); if (message.trim() && !busy && !offline) onSend(event) } }} />
      <div className="composer-actions"><button className="button button-quiet" type="button" onClick={() => onVoice()} disabled={!voice.enabled || offline}><Icon name="mic" size={18} />语音通话</button><button className="icon-button send-button" type="submit" aria-label="发送消息" disabled={!message.trim() || !!busy || offline}><Icon name="send" size={18} /></button></div>
    </form>
    {!voice.enabled && <p className="helper-note conversation-voice-note">{voice.reason || '语音服务暂未就绪，可先用文字继续。'}</p>}
  </section>
}

function Activity({ project, voice, onVoice }) {
  const notifications = project.notifications || []
  const events = project.events || []
  return <div className="activity-view">
    <div className="conversation-intro"><span className="eyebrow">来自真实项目状态</span><h2>最近发生了什么</h2><p>进度来自已记录的事件。没有记录时，搭档不会猜测完成百分比。</p></div>
    <section className="surface activity-section">
      <SectionHeading eyebrow="需要留意" title="回访消息" aside={`${notifications.length} 条`} />
      {!notifications.length && <p className="muted empty-line">有方案、重要阻塞或交付结果时，消息会出现在这里。</p>}
      <div className="notification-list">{notifications.map((item, index) => <div className="notification" key={item.id || index}>
        <div className="notification-icon"><Icon name={item.kind === 'ready' ? 'check' : 'message'} size={17} /></div>
        <div className="notification-body"><strong>{item.text || '项目有新消息'}</strong><time>{formatTime(item.created_at)}</time></div>
        {voice.enabled && <button className="text-button" type="button" onClick={() => onVoice(item.id)}>进入通话<Icon name="chevron" size={15} /></button>}
      </div>)}</div>
    </section>
    <section className="surface activity-section">
      <SectionHeading eyebrow="执行记录" title="项目动态" aside={`${events.length} 条`} />
      {!events.length && <p className="muted empty-line">尚无执行事件。</p>}
      <div className="event-list">{events.map((event, index) => <div className="event" key={event.id || index}>
        <span className="event-dot" /><div><strong>{event.summary || event.kind || '项目状态更新'}</strong><small>{formatTime(event.created_at)}{event.kind ? ` · ${event.kind}` : ''}</small></div>
      </div>)}</div>
    </section>
  </div>
}

function App() {
  const [project, setProject] = useState(undefined)
  const [projects, setProjects] = useState([])
  const [showNewProject, setShowNewProject] = useState(false)
  const [folderName, setFolderName] = useState('')
  const [parentDir, setParentDir] = useState('')
  const [folderData, setFolderData] = useState(null)
  const [folderOpen, setFolderOpen] = useState(false)
  const [folderLoading, setFolderLoading] = useState(false)
  const [folderError, setFolderError] = useState('')
  const folderRequestRef = useRef(0)
  const projectRequestRef = useRef(0)
  const projectListRequestRef = useRef(0)
  const [online, setOnline] = useState(navigator.onLine ? null : false)
  const [voice, setVoice] = useState({ enabled: false, reason: '正在检查语音服务…' })
  const [voiceOpen, setVoiceOpen] = useState(false)
  const [voiceContext, setVoiceContext] = useState('')
  const [callState, setCallState] = useState('idle')
  const [micState, setMicState] = useState('unknown')
  const [callError, setCallError] = useState('')
  const [muted, setMuted] = useState(false)
  const clientRef = useRef(null)
  const audioRef = useRef(null)
  const callAttemptRef = useRef(0)
  const [tab, setTab] = useState('overview')
  const [idea, setIdea] = useState('')
  const [files, setFiles] = useState([])
  const [message, setMessage] = useState('')
  const [changeText, setChangeText] = useState('')
  const [changeReason, setChangeReason] = useState('')
  const [showChange, setShowChange] = useState(false)
  const [choice, setChoice] = useState('')
  const [decisionReason, setDecisionReason] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [toast, setToast] = useState('')

  const refresh = useCallback(async ({ silent = false } = {}) => {
    const request = ++projectRequestRef.current
    try {
      const data = await api('/api/project')
      if (request !== projectRequestRef.current) return
      setProject(data && typeof data === 'object' && 'project' in data ? data.project : data || null)
      setOnline(true)
      if (!silent) setError('')
    } catch (failure) {
      if (request !== projectRequestRef.current) return
      setOnline(false)
      if (!silent) setError(failure.message)
    }
  }, [])

  const refreshProjects = useCallback(async () => {
    const request = ++projectListRequestRef.current
    try {
      const data = await api('/api/projects')
      if (request !== projectListRequestRef.current) return
      setProjects(Array.isArray(data?.projects) ? data.projects : [])
    } catch { /* The active project can still be used while the list is unavailable. */ }
  }, [])

  const loadFolders = useCallback(async (path) => {
    const request = ++folderRequestRef.current
    setFolderLoading(true)
    setFolderError('')
    try {
      const data = await api(`/api/folders${path ? `?path=${encodeURIComponent(path)}` : ''}`)
      if (request !== folderRequestRef.current) return
      setFolderData(data)
      setParentDir((current) => current || data.default_path || data.path || '')
    } catch (failure) {
      if (request === folderRequestRef.current) setFolderError(failure.message)
    } finally {
      if (request === folderRequestRef.current) setFolderLoading(false)
    }
  }, [])

  const checkHealth = useCallback(async () => {
    if (!navigator.onLine) { setOnline(false); return }
    try {
      const response = await fetch('/api/health', { cache: 'no-store', signal: AbortSignal.timeout(5000) })
      setOnline(response.ok)
    } catch { setOnline(false) }
  }, [])

  const checkVoice = useCallback(async () => {
    try {
      const config = await api('/api/voice/status')
      const enabled = config?.enabled === true || config?.available === true
      const missing = Array.isArray(config?.missing) ? config.missing.join('、') : ''
      setVoice({ enabled, reason: config?.reason || (enabled ? '' : missing || '语音服务缺少必要配置或尚未启动。') })
    } catch {
      setVoice({ enabled: false, reason: '语音服务未就绪，或所需配置尚未填写。' })
    }
  }, [])

  useEffect(() => {
    refresh()
    refreshProjects()
    loadFolders()
    checkHealth()
    checkVoice()
    const interval = window.setInterval(() => {
      if (!document.hidden) { refresh({ silent: true }); refreshProjects(); checkHealth(); checkVoice() }
    }, 7000)
    const onOnline = () => { checkHealth(); refresh({ silent: true }); refreshProjects(); loadFolders() }
    const onOffline = () => setOnline(false)
    window.addEventListener('online', onOnline)
    window.addEventListener('offline', onOffline)
    return () => {
      window.clearInterval(interval)
      window.removeEventListener('online', onOnline)
      window.removeEventListener('offline', onOffline)
    }
  }, [refresh, refreshProjects, loadFolders, checkHealth, checkVoice])

  useEffect(() => {
    setChoice('')
    setDecisionReason('')
  }, [project?.pending_question?.id])

  useEffect(() => {
    if (!toast) return
    const timeout = window.setTimeout(() => setToast(''), 4000)
    return () => window.clearTimeout(timeout)
  }, [toast])

  useEffect(() => () => {
    if (clientRef.current) clientRef.current.disconnect().catch(() => {})
  }, [])

  const offline = online === false
  const availableProjects = project?.id && !projects.some((item) => item.id === project.id)
    ? [{ id: project.id, idea: project.idea, folder_name: project.folder_name, phase: project.phase }, ...projects]
    : projects
  const lastEvent = project?.events?.[0]
  const hasPlan = !!project?.plan
  const canApprovePlan = project?.phase === 'PLAN_REVIEW' && project?.plan?.status === 'pending' && project?.plan?.spec_version === project?.spec_version
  const previousPlans = (project?.plan_history || [])
    .filter((item) => item.spec_version < project.spec_version)
    .sort((a, b) => b.spec_version - a.spec_version)
  const previewHref = project?.preview_url && safeLink(project.preview_url)
  const screen = useMemo(() => {
    if (!project) return 'new'
    if (project.pending_question) return 'decision'
    if (canApprovePlan) return 'plan'
    if (project.phase === 'READY') return 'ready'
    if (project.phase === 'FAILED') return 'failed'
    if (project.phase === 'PAUSED') return 'paused'
    return 'working'
  }, [project, canApprovePlan])

  async function mutate(key, action, success, after) {
    setBusy(key)
    setError('')
    try {
      await action()
      if (after) after()
      await refresh({ silent: true })
      await refreshProjects()
      if (success) setToast(success)
    } catch (failure) {
      setError(failure.message)
      await refresh({ silent: true })
    } finally {
      setBusy('')
    }
  }

  async function uploadAssets(picked) {
    const types = new Set(['image/png', 'image/jpeg', 'image/webp'])
    for (const file of picked) {
      if (!types.has(file.type)) throw new Error('只支持 PNG、JPEG 或 WebP 截图。')
      if (file.size > 5 * 1024 * 1024) throw new Error('截图不能超过 5 MB。')
      const form = new FormData()
      form.append('file', file)
      await api('/api/assets', { method: 'POST', body: form })
    }
  }

  function handleStart(event) {
    event.preventDefault()
    if ((!idea.trim() && !files.length) || !parentDir) return
    mutate('start', async () => {
      const startingIdea = idea.trim() || '请参考上传的截图，一起讨论我想做的作品。'
      await api('/api/project', { method: 'POST', body: JSON.stringify({ idea: startingIdea, parent_dir: parentDir, folder_name: folderName.trim() }) })
      setShowNewProject(false)
      if (files.length) await uploadAssets(files)
      await api('/api/messages', { method: 'POST', body: JSON.stringify({ text: startingIdea, channel: 'text' }) })
    }, '项目已创建，可以开始讨论。', () => { setFiles([]); setFolderName(''); setIdea(''); setTab('overview') })
  }

  async function handleVoiceStart() {
    if (!voice.enabled || !parentDir || offline || busy) return
    const startingIdea = idea.trim() || (files.length
      ? '请参考上传的截图，和我用语音讨论想做的作品。'
      : '我想通过语音描述一个网页作品，请先和我澄清想法。')
    setBusy('start')
    setError('')
    try {
      await api('/api/project', { method: 'POST', body: JSON.stringify({ idea: startingIdea, parent_dir: parentDir, folder_name: folderName.trim() }) })
      setShowNewProject(false)
      if (files.length) await uploadAssets(files)
      await api('/api/messages', { method: 'POST', body: JSON.stringify({ text: startingIdea, channel: 'text' }) })
      setFiles([])
      setFolderName('')
      setIdea('')
      await refresh({ silent: true })
      await refreshProjects()
      openVoice()
    } catch (failure) {
      setError(failure.message)
      await refresh({ silent: true })
    } finally {
      setBusy('')
    }
  }

  async function handleNewProject() {
    await closeVoice()
    setShowNewProject(true)
    setIdea('')
    setFiles([])
    setFolderName('')
    setParentDir(folderData?.default_path || '')
    setTab('overview')
    setError('')
    if (!folderData?.default_path) loadFolders()
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  async function handleSelectProject(event) {
    const projectId = event.target.value
    if (!projectId || projectId === project?.id || busy) return
    await closeVoice()
    mutate('select-project', () => api('/api/projects/select', {
      method: 'POST', body: JSON.stringify({ project_id: projectId }),
    }), '已切换项目。', () => {
      setShowNewProject(false)
      setTab('overview')
      setMessage('')
      setShowChange(false)
    })
  }

  function openFolderPicker() {
    setFolderOpen(true)
    setFolderData(null)
    loadFolders(parentDir || undefined)
  }

  function handleUpload(event) {
    const picked = Array.from(event.target.files || [])
    event.target.value = ''
    if (!picked.length) return
    mutate('upload', () => uploadAssets(picked), '参考截图已加入项目。')
  }

  function handleSend(event) {
    event.preventDefault()
    const text = message.trim()
    if (!text) return
    mutate('message', () => api('/api/messages', { method: 'POST', body: JSON.stringify({ text, channel: 'text' }) }), '', () => setMessage(''))
  }

  function handleAskReason() {
    if (busy || offline) return
    setTab('chat')
    const text = '为什么出错了？请检查当前项目状态和错误记录；能自动修复就修复，否则告诉我具体该怎么做。'
    mutate('diagnose', () => api('/api/messages', { method: 'POST', body: JSON.stringify({ text, channel: 'text' }) }))
  }

  function handleApprove() {
    mutate('approve', () => api('/api/plan/approve', { method: 'POST' }), '方案已确认，搭档将开始制作。')
  }

  function handleDecision() {
    if (!choice || !project?.pending_question) return
    const id = encodeURIComponent(project.pending_question.id)
    mutate('decision', () => api(`/api/decisions/${id}/answer`, {
      method: 'POST',
      body: JSON.stringify({ option_id: choice, reason: decisionReason.trim() || undefined }),
    }), '决定已记录，原任务会继续。')
  }

  function handleChange(event) {
    event.preventDefault()
    const text = changeText.trim()
    if (!text) return
    mutate('change', () => api('/api/changes', {
      method: 'POST', body: JSON.stringify({ text, reason: changeReason.trim() || '用户在项目运行中调整需求' }),
    }), '变更已记录，搭档会在安全边界调整。', () => { setChangeText(''); setChangeReason(''); setShowChange(false) })
  }

  function handleResume() {
    mutate('resume', () => api('/api/build/resume', { method: 'POST' }), '任务已恢复，页面会持续同步最新进度。')
  }

  function openVoice(contextId) {
    setVoiceContext(contextId || '')
    setCallError('')
    setVoiceOpen(true)
  }

  async function disconnectVoice() {
    callAttemptRef.current += 1
    const client = clientRef.current
    clientRef.current = null
    if (client) {
      setCallState('disconnecting')
      try { await client.disconnect() } catch { /* Local media is released below. */ }
    }
    if (audioRef.current) audioRef.current.srcObject = null
    setCallState('idle')
    setMuted(false)
  }

  async function closeVoice() {
    await disconnectVoice()
    setVoiceOpen(false)
  }

  async function connectVoice() {
    if (!voice.enabled || !project?.id || offline || clientRef.current) return
    const attempt = ++callAttemptRef.current
    setCallError('')
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setMicState('unsupported')
      setCallState('error')
      setCallError('麦克风需要 HTTPS 安全页面。请从私有 HTTPS 地址打开项目。')
      return
    }

    setCallState('connecting')
    let PipecatClient
    let SmallWebRTCTransport
    try {
      ;[{ PipecatClient }, { SmallWebRTCTransport }] = await Promise.all([
        import('@pipecat-ai/client-js'),
        import('@pipecat-ai/small-webrtc-transport'),
      ])
    } catch {
      setCallState('error')
      setCallError('无法加载语音通话组件。请保持连接并重新打开页面。')
      return
    }
    if (attempt !== callAttemptRef.current) return
    const client = new PipecatClient({
      transport: new SmallWebRTCTransport(),
      enableCam: false,
      enableMic: true,
      callbacks: {
        onConnected: () => setCallState('connecting'),
        onBotReady: () => setCallState('connected'),
        onDisconnected: () => setCallState('idle'),
        onTransportStateChanged: (state) => {
          if (state === 'connecting') setCallState('connecting')
          if (state === 'error') setCallState('error')
        },
        onDeviceError: (deviceError) => {
          setMicState('denied')
          setCallError(deviceError?.message || '无法访问麦克风。请在浏览器设置中允许此站点使用麦克风。')
        },
        onError: (event) => {
          setCallState('error')
          setCallError(event?.data?.message || event?.data?.error || '通话连接出现问题。')
        },
        onTrackStarted: (track, participant) => {
          if (track.kind !== 'audio' || participant?.local || !audioRef.current) return
          audioRef.current.srcObject = new MediaStream([track])
          audioRef.current.play().catch(() => setCallError('接收到了搭档的音频，但浏览器阻止播放。请点一下页面后重试。'))
        },
        onTrackStopped: (track, participant) => {
          if (track.kind === 'audio' && !participant?.local && audioRef.current) audioRef.current.srcObject = null
        },
      },
    })
    clientRef.current = client
    try {
      setCallState('requesting')
      setMicState('requesting')
      await client.initDevices()
      if (attempt !== callAttemptRef.current) { await client.disconnect(); return }
      setMicState('granted')
      setCallState('connecting')
      let connectionTimer
      await Promise.race([client.connect({
        webrtcRequestParams: {
          endpoint: '/api/voice/offer',
          requestData: { project_id: project.id },
        },
      }), new Promise((_, reject) => {
        connectionTimer = window.setTimeout(() => reject(new Error('语音连接超时。请确认运行服务的电脑在线，并重新尝试。')), 25000)
      })]).finally(() => window.clearTimeout(connectionTimer))
      if (attempt !== callAttemptRef.current) { await client.disconnect(); return }
      setCallState('connected')
    } catch (failure) {
      setCallState('error')
      const detail = failure?.message || String(failure)
      if (failure?.name === 'NotAllowedError' || /permission|denied/i.test(detail)) {
        setMicState('denied')
        setCallError('麦克风权限被拒绝。请在浏览器设置中允许此站点使用麦克风。')
      } else if (/503|unavailable|not configured/i.test(detail)) {
        setCallError('语音服务暂不可用，请检查运行服务的电脑上的语音配置。')
        checkVoice()
      } else {
        setCallError(`连接失败：${detail}`)
      }
      if (clientRef.current === client) clientRef.current = null
      try { await client.disconnect() } catch { /* Ignore cleanup errors. */ }
    }
  }

  function toggleMute() {
    const client = clientRef.current
    if (!client || callState !== 'connected') return
    const next = !muted
    client.enableMic(!next)
    setMuted(next)
    setMicState(next ? 'muted' : 'granted')
  }

  return <div className="app-shell">
    <header className="app-header">
      <div className="header-inner">
        <div className="brand"><span className="brand-mark"><span /><span /><span /><span /></span><div><strong>Movo</strong><small>VOICE TO PROTOTYPE</small></div></div>
        <StatusBadge online={online} phase={project?.phase} />
      </div>
    </header>

    {offline && <div className="offline-banner" role="status"><Icon name="alert" size={18} /><span>主机离线或无法连接。电脑恢复在线后，此页会自动同步项目状态。</span><button type="button" onClick={() => { checkHealth(); refresh() }}>重试</button></div>}
    {error && <div className="error-banner" role="alert"><Icon name="alert" size={18} /><span>{error}</span><button type="button" aria-label="关闭错误提示" onClick={() => setError('')}><Icon name="close" size={17} /></button></div>}
    {toast && <div className="toast" role="status"><Icon name="check" size={17} />{toast}</div>}

    <main className="main">
      {project !== undefined && <div className="project-toolbar">
        <div className="project-switcher"><label htmlFor="active-project">项目空间</label><select id="active-project" value={showNewProject ? '' : (project?.id || '')} onChange={handleSelectProject} disabled={offline || !!busy || !availableProjects.length}>
          <option value="" disabled>{availableProjects.length ? '选择已有项目' : '暂无项目'}</option>
          {availableProjects.map((item) => <option value={item.id} key={item.id}>{item.folder_name || String(item.idea || '未命名项目').slice(0, 36)} · {phaseNames[item.phase] || item.phase || '进行中'}</option>)}
        </select></div>
        <button className="button button-secondary new-project-button" type="button" onClick={handleNewProject} disabled={offline || !!busy || showNewProject}><Icon name="plus" size={17} />新建项目</button>
      </div>}
      {project === undefined ? (offline
        ? <div className="offline-state"><span className="modal-icon"><Icon name="alert" size={23} /></span><h1>主机当前离线</h1><p>页面已保留在手机里，但需要运行服务的电脑恢复在线才能读取项目、通话或继续制作。</p><button className="button button-secondary" type="button" onClick={() => { checkHealth(); refresh() }}><Icon name="refresh" size={17} />重新连接</button></div>
        : <div className="loading-state"><span className="loading-orb" /><p>正在连接你的项目…</p></div>) : showNewProject || !project ? <StartView idea={idea} setIdea={setIdea} files={files} setFiles={setFiles} folderName={folderName} setFolderName={setFolderName} parentDir={parentDir} folderLoading={folderLoading} onChooseFolder={openFolderPicker} existingProject={!!project} onCancel={() => setShowNewProject(false)} busy={busy} offline={offline} onStart={handleStart} voice={voice} onVoice={handleVoiceStart} /> : <>
        <div className="project-head">
          <div className="project-heading"><span className="eyebrow">当前项目 <span className="project-id">#{String(project.id).slice(0, 8)}</span></span><h1>{project.idea || '我的网页原型'}</h1><div className="project-meta"><span>需求版本 v{project.spec_version || 1}</span><span className="meta-separator" /><span>{phaseNames[project.phase] || project.phase || '进行中'}</span></div>{project.source_dir && <div className="project-location" title={project.source_dir}>存放位置：{project.source_dir}</div>}</div>
          <button className={`button voice-entry ${voice.enabled ? '' : 'voice-entry-muted'}`} type="button" onClick={() => openVoice()} disabled={offline}><Icon name="mic" size={19} /><span>{voice.enabled ? '进入通话' : '语音待配置'}</span></button>
        </div>

        <nav className="tab-nav" aria-label="项目页面">
          {[['overview', '项目概览', 'home'], ['chat', '讨论', 'message'], ['activity', '动态', 'activity']].map(([key, label, icon]) => <button key={key} type="button" className={tab === key ? 'selected' : ''} onClick={() => setTab(key)} aria-current={tab === key ? 'page' : undefined}><Icon name={icon} size={18} />{label}</button>)}
        </nav>

        <section className="surface state-card"><div className="state-head"><span className="eyebrow">项目进度</span><span className="phase-indicator"><span className="pulse-dot" />{phaseNames[project.phase] || project.phase}</span></div><BuildProgress progress={project.progress} /><ProjectTimeline phase={project.phase} /></section>

        {tab === 'overview' && <div className="overview">
          <div className="overview-grid">
            <div className="primary-column">
              {screen === 'decision' && <DecisionCard question={project.pending_question} choice={choice} setChoice={setChoice} reason={decisionReason} setReason={setDecisionReason} busy={busy} offline={offline} onSubmit={handleDecision} />}
              {screen === 'plan' && <PlanCard plan={project.plan} busy={busy} offline={offline} onApprove={handleApprove} canApprove={canApprovePlan} />}
              {screen === 'ready' && <section className="surface next-card ready-card"><span className="card-kicker"><Icon name="check" size={17} />作品已就绪</span><h2>你的首版原型可以验收了。</h2><p>打开手机预览，试试核心操作，再告诉搭档要修改什么。</p>{previewHref && <a className="button button-primary" href={previewHref} target="_blank" rel="noopener noreferrer">打开作品预览<Icon name="external" size={18} /></a>}</section>}
              {screen === 'failed' && <section className="surface next-card fail-card"><span className="card-kicker"><Icon name="alert" size={17} />执行遇到问题</span><h2>搭档需要处理这个阻塞。</h2><p>{project.last_error || '失败原因尚未记录。可以在讨论中询问最新情况。'}</p><div className="next-actions"><button className="button button-primary" type="button" onClick={handleResume} disabled={!!busy || offline}><Icon name="refresh" size={17} />{busy === 'resume' ? '正在重试…' : '重试任务'}</button><button className="button button-secondary" type="button" onClick={handleAskReason} disabled={!!busy || offline}>询问原因<Icon name="arrow" size={18} /></button></div></section>}
              {screen === 'paused' && <section className="surface next-card paused-card"><span className="card-kicker"><Icon name="clock" size={17} />任务已暂停</span><h2>可以从当前项目继续。</h2><p>{lastEvent?.summary || '项目状态已保存。继续后会按当前需求版本恢复任务或更新方案。'}</p><div className="next-actions"><button className="button button-primary" type="button" onClick={handleResume} disabled={!!busy || offline}><Icon name="arrow" size={17} />{busy === 'resume' ? '正在恢复…' : '继续任务'}</button><button className="button button-secondary" type="button" onClick={() => setTab('chat')}>询问进度</button></div></section>}
              {screen === 'working' && <section className="surface next-card working-card"><span className="card-kicker"><Icon name="spark" size={17} />现在进行中</span><h2>{phaseNames[project.phase] || '搭档正在处理项目'}</h2><p>{project.progress?.step || lastEvent?.summary || '当前还没有新的执行记录。你可以随时询问进度或补充想法。'}</p><div className="next-actions"><button className="button button-secondary" type="button" onClick={() => setTab('chat')}>询问进度<Icon name="arrow" size={17} /></button>{voice.enabled && <button className="button button-quiet" type="button" onClick={() => openVoice()} disabled={offline}><Icon name="mic" size={17} />进入通话</button>}</div></section>}
              {hasPlan && screen !== 'plan' && <PlanCard plan={project.plan} busy={busy} offline={offline} onApprove={handleApprove} canApprove={canApprovePlan} />}

              <section className="surface brief-card"><SectionHeading eyebrow="创作起点" title="想法与参考" /><p className="project-idea">{project.idea || '尚未记录想法'}</p><div className="screenshots">{(project.screenshots || []).map((shot, index) => <a key={shot.id || index} className="shot" href={safeLink(shot.url)} target="_blank" rel="noopener noreferrer"><img src={shot.url} alt={shot.name || `参考截图 ${index + 1}`} /><span>{shot.name || `参考截图 ${index + 1}`}</span></a>)}</div><label className={`upload-add ${offline || !!busy ? 'disabled' : ''}`} htmlFor="project-upload"><input id="project-upload" type="file" accept="image/png,image/jpeg,image/webp" multiple disabled={offline || !!busy} onChange={handleUpload} /><Icon name="upload" size={17} />{busy === 'upload' ? '正在上传…' : '补充参考截图'}</label></section>

              <section className="surface change-card"><SectionHeading eyebrow="方向可以调整" title="修改这个版本" />{!showChange ? <><p className="muted">想删掉某个功能或改变重点？变更会生成新的需求版本，并保留先前方案。</p><button className="button button-secondary" type="button" onClick={() => setShowChange(true)}><Icon name="plus" size={17} />提出修改</button></> : <form onSubmit={handleChange}><label className="field-label" htmlFor="change-text">希望怎么改？</label><textarea id="change-text" className="input" rows="3" value={changeText} onChange={(event) => setChangeText(event.target.value)} placeholder="例如：先删掉支付功能，只保留可试用的核心流程。" /><label className="field-label" htmlFor="change-reason">为什么调整？ <span>可选</span></label><input id="change-reason" className="input" value={changeReason} onChange={(event) => setChangeReason(event.target.value)} placeholder="例如：先验证是否有人愿意使用" /><div className="form-actions"><button type="button" className="button button-quiet" onClick={() => setShowChange(false)}>取消</button><button type="submit" className="button button-primary" disabled={!changeText.trim() || !!busy || offline}>{busy === 'change' ? '正在记录…' : '记录需求变更'}</button></div></form>}</section>
            </div>

            <aside className="side-column">
              <section className="surface side-card"><SectionHeading eyebrow="最近动作" title="真实进度" /><p className="recent-summary">{lastEvent?.summary || '还没有执行事件。'}</p>{lastEvent && <time>{formatTime(lastEvent.created_at)}</time>}<button type="button" className="text-button" onClick={() => setTab('activity')}>查看全部动态<Icon name="chevron" size={15} /></button></section>
              <section className="surface side-card"><SectionHeading eyebrow="可交付结果" title="预览与验证" />{previewHref ? <a className="preview-link" href={previewHref} target="_blank" rel="noopener noreferrer"><div className="preview-art"><span className="preview-window"><i /><i /><i /></span><Icon name="external" size={20} /></div><strong>打开手机预览</strong><small>在新页面体验首版作品</small></a> : <p className="muted">预览尚未生成。构建完成后入口会出现在这里。</p>}<div className="verification-list"><div><span>构建</span><strong>{project.verification?.build_status || '待验证'}</strong></div><div><span>核心交互</span><strong>{project.verification?.interaction_status || '待验证'}</strong></div></div>{project.verification?.details && <p className="verification-details">{project.verification.details}</p>}</section>
              <section className="surface side-card"><SectionHeading eyebrow="通话状态" title="随时回来聊" /><p className="muted">通话会读取最新项目状态；消息和文字决定也写回这项任务。</p><button className="button button-secondary full-button" type="button" onClick={() => openVoice()} disabled={offline}><Icon name="mic" size={17} />{voice.enabled ? '进入项目通话' : '查看语音状态'}</button>{!voice.enabled && <p className="helper-note">{voice.reason || '语音服务尚未就绪。'}</p>}</section>
              {!!previousPlans.length && <section className="surface side-card"><SectionHeading eyebrow="可追溯版本" title="过去的方案" /><div className="plan-history">{previousPlans.map((item) => <div key={item.id || item.spec_version}><span>v{item.spec_version}</span><strong>{item.goal || '较早版本方案'}</strong></div>)}</div></section>}
            </aside>
          </div>
        </div>}

        {tab === 'chat' && <Conversation project={project} message={message} setMessage={setMessage} busy={busy} offline={offline} onSend={handleSend} voice={voice} onVoice={openVoice} />}
        {tab === 'activity' && <Activity project={project} voice={voice} onVoice={openVoice} />}
      </>}
    </main>

    <footer className="app-footer"><span>Movo · 本机个人项目</span><span>运行服务的电脑保持在线，手机才能继续连接</span></footer>
    <audio ref={audioRef} autoPlay playsInline aria-hidden="true" />
    {folderOpen && <FolderPicker folders={folderData} loading={folderLoading} error={folderError} onNavigate={loadFolders} onSelect={(path) => { if (!path) return; setParentDir(path); setFolderOpen(false) }} onClose={() => setFolderOpen(false)} />}
    {voiceOpen && <div className="modal-backdrop" role="presentation" onClick={closeVoice}><div className="modal voice-modal" role="dialog" aria-modal="true" aria-labelledby="voice-title" onClick={(event) => event.stopPropagation()}><button className="modal-close" type="button" aria-label="关闭通话面板" onClick={closeVoice}><Icon name="close" size={19} /></button><span className="modal-icon"><Icon name="mic" size={23} /></span><span className="eyebrow">项目通话 {voiceContext ? '· 来自回访消息' : ''}</span><h2 id="voice-title">和搭档聊聊</h2><p>通话中可直接开口打断搭档；搭档会停下，听完后根据对话继续。语速为 1.2 倍。已确认的需求与决定仍留在同一任务。</p><div className="call-status"><span className={`call-status-dot ${callState === 'connected' ? 'live' : ''}`} /><div><strong>{{ idle: '尚未接通', requesting: '正在请求麦克风权限', connecting: '正在建立连接', connected: '通话中', disconnecting: '正在挂断', error: '通话未连接' }[callState]}</strong><small>{{ unknown: '麦克风状态待检查', requesting: '请允许浏览器使用麦克风', granted: '麦克风已授权', denied: '麦克风权限未获得', unsupported: '此页面不能使用麦克风', muted: '麦克风已静音' }[micState]}</small></div></div>{(callError || offline || !voice.enabled) && <div className="call-warning" role="alert">{callError || (offline ? '运行服务的电脑离线，请恢复连接后再试。' : voice.reason || '语音服务尚未就绪。')}</div>}<div className="call-actions">{callState === 'connected' ? <><button className="button button-quiet" type="button" onClick={toggleMute}>{muted ? '取消静音' : '静音'}</button><button className="button button-hangup" type="button" onClick={disconnectVoice}>挂断通话</button></> : <button className="button button-primary full-button" type="button" onClick={connectVoice} disabled={!voice.enabled || !project?.id || offline || ['requesting', 'connecting', 'disconnecting'].includes(callState)}>{callState === 'requesting' || callState === 'connecting' ? '正在连接…' : '开始通话'}</button>}</div>{!project && <p className="helper-note centered">请先创建项目，再进入带上下文的语音通话。</p>}</div></div>}
  </div>
}

export default App
