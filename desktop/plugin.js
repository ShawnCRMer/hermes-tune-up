import * as sdk from '@hermes/plugin-sdk'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs, Fragment } from 'react/jsx-runtime'

// Namespace import: a missing optional export on an older host must not break loading.
const { Button, Popover, PopoverContent, PopoverTrigger, STATUSBAR_AREAS, useGatewayActivationEpoch, useQuery, useQueryClient, useValue, host } = sdk
const ROUTES_AREA = sdk.ROUTES_AREA
const PALETTE_AREA = sdk.PALETTE_AREA
const SIDEBAR_NAV_AREA = sdk.SIDEBAR_NAV_AREA

const ID = 'storage-utility'
const VERSION = '0.7.3'
// The oldest server part these screens work with. Bump only when the backend changes, so a
// screens-only update never asks for a restart it doesn't need.
const SERVER_NEEDS = '0.7.1'
function olderThan(version, needed) {
  if (!version) return true
  const a = String(version).split('.').map(Number), b = needed.split('.').map(Number)
  for (let i = 0; i < 3; i += 1) { if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) < (b[i] || 0) }
  return false
}
const ROUTE = '/storage'
const AUTO_SCAN_AGE_MS = 24 * 3600 * 1000
const OPEN_SCAN_AGE_MS = 3600 * 1000
let CTX = null
let closeFull = null

// The full view opens as a closeable tab docked to the RIGHT of the chat (✕ or ⌘W closes it),
// never by replacing the session. Older desktops without openWorkspace fall back to the page.
function canDock() {
  return typeof host.openWorkspace === 'function'
}

function openFull() {
  if (!canDock()) { host.navigate(ROUTE); return false }
  closeFull = host.openWorkspace('storage', {
    title: 'Tune-up',
    dock: { pane: 'workspace', pos: 'right' },
    minWidth: '26rem',
    render: () => jsx(StoragePage, { onClose: () => { if (closeFull) { const close = closeFull; closeFull = null; close() } } }),
    onClose: () => { closeFull = null }
  })
  return true
}
let lastAutoStart = 0

const CSS = `
.su-trigger{display:flex;align-items:center;gap:5px;height:100%;padding:0 7px;color:var(--ui-text-tertiary);font-size:11px;white-space:nowrap;background:transparent;border:0;cursor:pointer}
.su-trigger:hover{color:var(--ui-text-primary);background:var(--chrome-action-hover)}
.su-trigger strong{font-variant-numeric:tabular-nums;color:var(--ui-text-secondary);font-weight:500}
.su-trigger.su-low strong{color:var(--ui-warning,#d29922)}
.su-panel{width:410px;max-width:calc(100vw - 20px);height:min(680px,calc(100dvh - 28px));display:flex;flex-direction:column;overflow:hidden;padding:14px 14px 0;color:var(--ui-text-primary)}
.su-panel-body{flex:1 1 auto;min-height:0;overflow:auto;padding-bottom:14px;margin:0 -6px;padding-left:6px;padding-right:6px}
.su-chat{display:block;margin-top:2px;font-size:11.5px;color:var(--ui-text-secondary);white-space:normal}
.su-chat b{font-weight:600;color:var(--ui-text-primary)}
.su-advice{margin-top:3px;font-size:11.5px;white-space:normal}
.su-advice b{font-weight:600}
.su-page{height:100%;overflow:auto;color:var(--ui-text-primary);container-type:inline-size}
.su-page-inner{display:grid;grid-template-columns:minmax(330px,410px) minmax(0,1fr);gap:24px;max-width:1320px;padding:22px 26px}
.su-page-inner>section{min-width:0}
@media(max-width:900px){.su-page-inner{grid-template-columns:1fr;padding:14px}}
@container (max-width:820px){.su-page-inner{grid-template-columns:1fr;padding:14px}}
.su-titlerow{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.su-full{all:unset;cursor:pointer;flex-shrink:0;font-size:12px;padding:4px 8px;border-radius:6px;border:1px solid var(--ui-stroke-secondary);color:var(--ui-text-secondary);white-space:nowrap}
.su-full:hover{color:var(--ui-text-primary);background:color-mix(in srgb,var(--ui-text-primary) 6%,transparent)}
.su h2{margin:0;font-size:17px;line-height:24px;font-weight:600}
.su h3{margin:0;font-size:13px;line-height:19px;font-weight:600}
.su h4{margin:14px 0 4px;font-size:11px;font-weight:600;color:var(--ui-text-secondary);text-transform:uppercase;letter-spacing:.04em}
.su p{margin:4px 0}
.su-meta{color:var(--ui-text-tertiary);font-size:11px;line-height:16px}
.su-muted{color:var(--ui-text-secondary);font-size:12px;line-height:18px}
.su-tabs{display:flex;gap:3px;overflow:auto;margin:12px 0 10px}
.su-tabs button{border:0;border-radius:6px;padding:5px 9px;background:transparent;color:var(--ui-text-secondary);font-size:12px;white-space:nowrap;cursor:pointer}
.su-tabs button[aria-selected="true"]{background:color-mix(in srgb,var(--ui-text-primary) 9%,transparent);color:var(--ui-text-primary)}
.su-capacity{display:grid;grid-template-columns:1fr auto;align-items:end;gap:2px 10px}
.su-capacity strong{font-size:17px;font-weight:650;font-variant-numeric:tabular-nums}
.su-bar{grid-column:1/-1;height:6px;border-radius:3px;background:color-mix(in srgb,var(--ui-text-primary) 10%,transparent);overflow:hidden;display:flex;margin-top:5px}
.su-bar i{display:block;height:100%}
.su-bar .su-hermes{background:var(--ui-accent)}
.su-bar .su-other{background:color-mix(in srgb,var(--ui-text-primary) 28%,transparent)}
.su-stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:14px 0 8px}
.su-stats strong{display:block;font-size:22px;font-weight:650;font-variant-numeric:tabular-nums;line-height:28px}
.su-scanline{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:11px;color:var(--ui-text-secondary);margin-bottom:10px}
.su-progress{height:3px;margin:4px 0 10px;background:var(--ui-stroke-secondary);overflow:hidden;border-radius:2px}
.su-progress:before{content:"";display:block;height:100%;width:40%;background:var(--ui-accent);animation:su-progress 1.2s ease-in-out infinite alternate}
@keyframes su-progress{from{transform:translateX(-40%)}to{transform:translateX(190%)}}
.su-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.su-card{all:unset;box-sizing:border-box;display:block;cursor:pointer;border:1px solid var(--ui-stroke-secondary);border-radius:8px;padding:10px;background:color-mix(in srgb,var(--ui-text-primary) 4%,transparent);min-width:0}
.su-card:hover,.su-card:focus-visible{border-color:color-mix(in srgb,var(--ui-accent) 60%,var(--ui-stroke-secondary))}
.su-card.su-attention{border-color:color-mix(in srgb,var(--ui-warning,#d29922) 55%,var(--ui-stroke-secondary))}
.su-card-title{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600;color:var(--ui-text-primary)}
.su-card strong{display:block;margin-top:4px;font-size:18px;font-weight:650;font-variant-numeric:tabular-nums}
.su-card .su-meta{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.su-rows{margin-top:8px;border-top:1px solid var(--ui-stroke-secondary)}
.su-row{all:unset;box-sizing:border-box;display:grid;grid-template-columns:16px minmax(0,1fr) auto;gap:8px;align-items:center;width:100%;padding:7px 2px;border-bottom:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 55%,transparent);font-size:12px;cursor:pointer}
.su-row:hover{background:color-mix(in srgb,var(--ui-text-primary) 4%,transparent)}
.su-row b{font-weight:500}
.su-row .su-meta{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.su-num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap;color:var(--ui-text-secondary)}
.su-actions{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin-top:12px}
.su-actions.su-end{justify-content:flex-end}
.su-actions.su-start{justify-content:flex-start}
.su-link{all:unset;cursor:pointer;font-size:12px;color:var(--ui-text-secondary)}
.su-link:hover{color:var(--ui-text-primary);text-decoration:underline}
.su-footer{margin-top:12px;padding-top:9px;border-top:1px solid var(--ui-stroke-secondary);font-size:11px;color:var(--ui-text-tertiary)}
.su-note{padding:8px 10px;border-radius:7px;background:color-mix(in srgb,var(--ui-text-primary) 5%,transparent);font-size:12px;line-height:18px;color:var(--ui-text-secondary);margin:8px 0}
.su-note.su-warn{background:color-mix(in srgb,var(--ui-warning,#d29922) 13%,transparent);color:var(--ui-text-primary)}
.su-note.su-ok{background:color-mix(in srgb,var(--ui-success,#3fb950) 13%,transparent);color:var(--ui-text-primary)}
.su-list{border:1px solid var(--ui-stroke-secondary);border-radius:7px;max-height:340px;overflow:auto}
.su-page .su-list{max-height:none}
.su-item{display:grid;grid-template-columns:18px minmax(0,1fr) auto;gap:8px;align-items:center;padding:6px 8px;border-bottom:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 55%,transparent);font-size:12px}
.su-item:last-child{border-bottom:0}
.su-item>span{min-width:0}
.su-item.su-click{cursor:pointer}
.su-item.su-click:hover{background:color-mix(in srgb,var(--ui-text-primary) 4%,transparent)}
.su-path{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.su-item.su-disabled{opacity:.62}
.su input[type=checkbox],.su input[type=radio]{width:14px;height:14px;margin:0;accent-color:var(--ui-accent)}
.su label.su-check{display:flex;gap:8px;align-items:flex-start;font-size:12px;line-height:17px;margin:8px 0}
.su label.su-check input{margin-top:2px;flex-shrink:0}
.su select{font-size:12px;color:var(--ui-text-primary);background:var(--ui-bg,transparent);border:1px solid var(--ui-stroke-secondary);border-radius:5px;padding:3px 5px}
.su pre,.su code{font-family:var(--font-mono,ui-monospace,monospace);font-size:11px}
.su pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:220px;overflow:auto;padding:8px;border-radius:6px;background:color-mix(in srgb,var(--ui-text-primary) 6%,transparent);margin:6px 0}
.su-cmd{border:1px solid var(--ui-stroke-secondary);border-radius:8px;padding:10px;margin:8px 0}
.su-cmd code{padding:1px 5px;border-radius:4px;background:color-mix(in srgb,var(--ui-text-primary) 8%,transparent)}
.su-step{display:grid;grid-template-columns:20px 1fr;gap:6px;margin:10px 0;font-size:12px;line-height:18px}
.su-step>b{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:9px;background:color-mix(in srgb,var(--ui-accent) 22%,transparent);font-size:10px}
.su-codeline{display:flex;gap:6px;align-items:center;margin-top:4px}
.su-codeline code{flex:1;padding:4px 7px;border-radius:5px;background:color-mix(in srgb,var(--ui-text-primary) 8%,transparent)}
.su-back{all:unset;cursor:pointer;font-size:12px;color:var(--ui-text-secondary);margin-bottom:8px;display:inline-block}
.su-crumbs{display:flex;flex-wrap:wrap;gap:2px;font-size:12px;margin:2px 0 6px;align-items:center}
.su-crumbs button{all:unset;cursor:pointer;color:var(--ui-text-secondary);padding:1px 3px;border-radius:4px}
.su-crumbs button:hover{color:var(--ui-text-primary);background:color-mix(in srgb,var(--ui-text-primary) 7%,transparent)}
.su-treemap{position:relative;width:100%;height:170px;border-radius:7px;overflow:hidden;margin:6px 0 10px;background:color-mix(in srgb,var(--ui-text-primary) 4%,transparent)}
.su-page .su-treemap{height:min(46vh,420px)}
.su-tile{all:unset;position:absolute;box-sizing:border-box;border:1px solid color-mix(in srgb,var(--ui-text-primary) 18%,transparent);padding:3px 5px;overflow:hidden;font-size:11px;line-height:14px;color:var(--ui-text-primary);text-align:left}
.su-tile.su-dir{cursor:pointer}
.su-tile.su-dir:hover{filter:brightness(1.2)}
.su-tile b{display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.su-tile span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ui-text-secondary)}
.su-desc{display:block;color:var(--ui-text-tertiary);font-size:11px;white-space:normal}
.su-badge{display:inline-block;margin-left:6px;padding:0 6px;border-radius:8px;font-size:10px;line-height:16px;background:color-mix(in srgb,var(--ui-accent) 18%,transparent);color:var(--ui-text-secondary);cursor:pointer;border:0}
.su-error{color:var(--ui-text-primary);background:color-mix(in srgb,var(--ui-danger,#f85149) 13%,transparent);padding:8px 10px;border-radius:7px;font-size:12px;margin:8px 0}
.su-radio{display:flex;gap:6px 14px;flex-wrap:wrap;font-size:12px;margin:8px 0}
.su-radio label{display:flex;gap:6px;align-items:center}
.su-head{font-size:17px;font-weight:650;line-height:23px;margin:2px 0 8px}
.su-head .su-word-low{color:var(--ui-warning,#d29922)}
.su-head .su-word-ok{color:var(--ui-success,#3fb950)}
.su-tbar{height:10px;border-radius:5px;background:color-mix(in srgb,var(--ui-text-primary) 10%,transparent);overflow:hidden;display:flex}
.su-tbar i{display:block;height:100%}
.su-g{background:var(--ui-success,#3fb950)}.su-bad{background:var(--ui-danger,#f85149)}.su-y{background:var(--ui-warning,#d29922)}.su-b{background:#4c8dde}.su-o{background:color-mix(in srgb,var(--ui-text-primary) 30%,transparent)}.su-k{background:color-mix(in srgb,var(--ui-text-primary) 45%,transparent)}
.su-legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:11px;color:var(--ui-text-secondary);margin:6px 0 12px}
.su-legend span{display:inline-flex;align-items:center;gap:5px}
.su-dot{display:inline-block;width:9px;height:9px;border-radius:50%;flex-shrink:0}
.su-say{font-size:14px;line-height:20px;margin:0 0 10px}
.su-say b{color:var(--ui-success,#3fb950)}
.su-big{all:unset;box-sizing:border-box;display:block;width:100%;text-align:center;padding:11px;border-radius:9px;background:var(--ui-success,#2f9e5f);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
.su-big:hover{filter:brightness(1.08)}
.su-big[disabled]{opacity:.5;cursor:default}
.su-sub{text-align:center;font-size:11px;color:var(--ui-text-tertiary);margin:5px 0 12px}
.su-group{border:1px solid var(--ui-stroke-secondary);border-radius:9px;margin-bottom:8px;overflow:hidden}
.su-ghead{all:unset;box-sizing:border-box;display:grid;grid-template-columns:10px minmax(0,1fr) auto 12px;gap:9px;align-items:center;width:100%;padding:9px 11px;cursor:pointer;background:color-mix(in srgb,var(--ui-text-primary) 4%,transparent)}
.su-ghead:hover{background:color-mix(in srgb,var(--ui-text-primary) 7%,transparent)}
.su-ghead b{display:block;font-size:13px;font-weight:600}
.su-ghead .su-meta{display:block}
.su-ghead strong{font-size:13px;font-variant-numeric:tabular-nums}
.su-chev{color:var(--ui-text-tertiary);transition:transform .15s}
.su-group.su-open .su-chev{transform:rotate(90deg)}
.su-gbody{padding:2px 11px 8px}
.su-frow{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:9px;padding:8px 0;border-top:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 60%,transparent);align-items:start}
.su-frow:first-child{border-top:0}
.su-frow input{margin-top:3px}
.su-frow b{font-size:13px;font-weight:600}
.su-frow p{font-size:12px;line-height:17px;color:var(--ui-text-secondary);margin:1px 0 0}
.su-frow .su-num{font-size:12px}
.su-frow.su-go{cursor:pointer}
.su-frow.su-go:hover{background:color-mix(in srgb,var(--ui-text-primary) 3%,transparent)}
.su-why{all:unset;cursor:pointer;font-size:11px;color:var(--ui-accent);margin-top:3px;display:inline-block}
.su-qa{margin-top:6px;padding:7px 9px;border-radius:7px;background:color-mix(in srgb,var(--ui-text-primary) 5%,transparent);font-size:12px;line-height:17px}
.su-qa div{display:grid;grid-template-columns:96px 1fr;gap:6px;margin:2px 0}
.su-qa span{color:var(--ui-text-tertiary)}
.su-tech{font-family:var(--font-mono,ui-monospace,monospace);font-size:10.5px;color:var(--ui-text-tertiary);margin-top:3px;overflow-wrap:anywhere}
.su-projhead{display:grid;grid-template-columns:16px minmax(0,1fr) auto;gap:8px;align-items:center;padding:7px 8px 5px;font-size:12px;background:color-mix(in srgb,var(--ui-text-primary) 5%,transparent);border-bottom:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 55%,transparent)}
.su-projhead b{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.su-from{font-size:11.5px!important;line-height:16px!important;color:var(--ui-text-tertiary)!important;margin-top:3px!important}
.su-from b{font-weight:600;color:var(--ui-text-secondary)}
.su-lock{font-size:12px;line-height:17px;color:var(--ui-text-secondary);margin-top:1px}
.su-done{text-align:center;padding:10px 4px}
.su-done .su-tick{width:46px;height:46px;border-radius:50%;margin:0 auto 6px;display:grid;place-items:center;font-size:24px;font-weight:700;color:#fff;background:var(--ui-success,#2f9e5f)}
.su-done h3{font-size:17px!important;line-height:24px!important}
.su-switch{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px;color:var(--ui-text-secondary);margin-top:12px;padding-top:9px;border-top:1px solid var(--ui-stroke-secondary)}
@media(max-width:460px){.su-panel{width:calc(100vw - 20px);padding:12px}.su-stats strong{font-size:19px}}
`

// ----- formatting ------------------------------------------------------------------

function bytes(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return 'unknown'
  const sign = Number(value) < 0 ? '-' : ''
  let amount = Math.abs(Number(value))
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let index = 0
  while (amount >= 1000 && index < units.length - 1) { amount /= 1000; index += 1 }
  const digits = index === 0 || amount >= 100 ? 0 : amount >= 10 ? 1 : (index >= 3 ? 2 : 1)
  return `${sign}${amount.toFixed(digits)} ${units[index]}`
}

function signed(value) {
  if (value === null || value === undefined) return 'unknown'
  return `${Number(value) > 0 ? '+' : ''}${bytes(value)}`
}

function ago(iso) {
  if (!iso) return 'never'
  const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000)
  if (!Number.isFinite(seconds)) return 'unknown'
  if (seconds < 90) return 'just now'
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 129600) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86400)} days ago`
}

function count(value, noun, plural) {
  const number = Number(value || 0)
  return `${number.toLocaleString()} ${number === 1 ? noun : plural || `${noun}s`}`
}

function errorText(error) {
  const raw = String(error?.message || error || 'Request failed')
  const match = raw.match(/^(\d{3}):\s*([\s\S]*)$/)
  if (match) {
    try {
      const body = JSON.parse(match[2])
      return body?.error?.message || body?.message || body?.detail || `Request failed (${match[1]})`
    } catch (_) {
      return `Request failed (${match[1]})`
    }
  }
  return raw
}

function rest(path, options) {
  if (!CTX) return Promise.reject(new Error('Tune-up is not registered'))
  return CTX.rest(path, options)
}

function sameBinding(left, right) {
  return Boolean(left && right) && JSON.stringify(left) === JSON.stringify(right)
}

function icon(name) {
  return jsx('span', { className: `codicon codicon-${name || 'circle-outline'}`, 'aria-hidden': 'true' })
}

function hue(text) {
  let value = 7
  for (const char of String(text || '')) value = (value * 31 + char.charCodeAt(0)) % 360
  return value
}

function toggleIn(set, id) {
  const next = new Set(set)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  return next
}

// ----- data ------------------------------------------------------------------------

function useRoute() {
  const epoch = useGatewayActivationEpoch()
  const profile = useValue(host.state.profile)
  const gateway = useValue(host.state.gateway)
  const connectionId = useValue(host.state.connectionId)
  // Never call the backend while the gateway is connecting, restarting or switching.
  return { scopeKey: `${epoch ?? ''}|${connectionId ?? ''}|${profile ?? ''}`, ready: gateway === 'open' && Boolean(connectionId) }
}

function busy(data) {
  return Boolean((data?.scan && !data.scan.finished) || data?.command?.state === 'running' || data?.projects?.running)
}

function useStorage(active) {
  const { scopeKey, ready } = useRoute()
  const queryClient = useQueryClient()
  const capabilities = useQuery({
    queryKey: [ID, 'capabilities', scopeKey],
    queryFn: () => rest('/capabilities'),
    enabled: ready,
    staleTime: 60000,
    refetchInterval: 300000,
    retry: false,
    refetchOnWindowFocus: false
  })
  const binding = ready ? capabilities.data?.binding || null : null
  const overview = useQuery({
    queryKey: [ID, 'overview', scopeKey],
    queryFn: () => rest('/overview', { timeoutMs: 180000 }),
    enabled: Boolean(binding) && active,
    refetchInterval: query => (busy(query.state.data) ? 1500 : 60000),
    retry: false,
    refetchOnWindowFocus: false
  })
  const data = overview.data
  // The backend answered for a different instance: re-attest before allowing actions.
  useEffect(() => {
    if (data?.binding && binding && !sameBinding(data.binding, binding)) void capabilities.refetch()
  }, [data?.binding, binding])

  const refresh = () => { void queryClient.invalidateQueries({ queryKey: [ID] }) }
  const post = async (path, body = {}, options = {}) => {
    if (!binding) throw new Error('Tune-up has not connected to this Hermes yet')
    return rest(path, { method: 'POST', body: { ...body, binding }, ...options })
  }
  const startScan = async () => {
    lastAutoStart = Date.now()
    await post('/scans')
    refresh()
  }
  // First scan, and a refresh when opened on a snapshot older than an hour.
  useEffect(() => {
    if (!active || !data || !binding || busy(data) || !sameBinding(data.binding, binding)) return
    const finished = data.snapshot?.finishedAt ? Date.parse(data.snapshot.finishedAt) : 0
    if (Date.now() - finished > OPEN_SCAN_AGE_MS && Date.now() - lastAutoStart > 10 * 60000) void startScan().catch(() => {})
  }, [active, data?.snapshot?.finishedAt, Boolean(data), binding])

  return { scopeKey, capabilities, binding, overview, data, post, refresh, startScan }
}

// ----- treemap ---------------------------------------------------------------------

function worst(row, side, scale) {
  let sum = 0
  let max = 0
  let min = Infinity
  for (const item of row) { const area = item.value * scale; sum += area; max = Math.max(max, area); min = Math.min(min, area) }
  if (!sum || !min) return Infinity
  return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min))
}

export function squarify(items, width, height) {
  const out = []
  let rest = items.filter(item => item.value > 0)
  let x = 0
  let y = 0
  let w = width
  let h = height
  while (rest.length && w > 0.5 && h > 0.5) {
    const total = rest.reduce((sum, item) => sum + item.value, 0)
    const scale = (w * h) / total
    const side = Math.min(w, h)
    const row = []
    let best = Infinity
    for (const item of rest) {
      const ratio = worst([...row, item], side, scale)
      if (row.length && ratio > best) break
      row.push(item)
      best = ratio
    }
    const rowValue = row.reduce((sum, item) => sum + item.value, 0)
    if (w >= h) {
      const rowWidth = (rowValue / total) * w
      let cursor = y
      for (const item of row) { const itemHeight = (item.value / rowValue) * h; out.push({ ...item, x, y: cursor, w: rowWidth, h: itemHeight }); cursor += itemHeight }
      x += rowWidth
      w -= rowWidth
    } else {
      const rowHeight = (rowValue / total) * h
      let cursor = x
      for (const item of row) { const itemWidth = (item.value / rowValue) * w; out.push({ ...item, x: cursor, y, w: itemWidth, h: rowHeight }); cursor += itemWidth }
      y += rowHeight
      h -= rowHeight
    }
    rest = rest.slice(row.length)
  }
  return out
}

function Treemap({ items, onOpen }) {
  const ref = useRef(null)
  const [size, setSize] = useState({ w: 380, h: 170 })
  useLayoutEffect(() => {
    const element = ref.current
    if (!element) return undefined
    const measure = () => setSize({ w: element.clientWidth || 380, h: element.clientHeight || 170 })
    measure()
    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const tiles = useMemo(() => squarify(items, size.w, size.h), [items, size.w, size.h])
  return jsx('div', {
    ref,
    className: 'su-treemap',
    role: 'img',
    'aria-label': 'Folder size map',
    children: tiles.map(tile => {
      const color = tile.kind === 'other'
        ? 'color-mix(in srgb,var(--ui-text-primary) 9%,transparent)'
        : `hsl(${hue(tile.name)} 55% 52% / ${tile.kind === 'dir' ? 0.38 : 0.2})`
      const small = tile.w < 46 || tile.h < 30
      return jsxs(tile.kind === 'dir' ? 'button' : 'div', {
        className: `su-tile ${tile.kind === 'dir' ? 'su-dir' : ''}`,
        style: { left: tile.x, top: tile.y, width: tile.w, height: tile.h, background: color },
        title: `${tile.label} · ${bytes(tile.value)}${tile.description ? `\n${tile.description}` : ''}`,
        onClick: tile.kind === 'dir' ? () => onOpen(tile.path) : undefined,
        children: small ? [] : [jsx('b', { children: tile.label }, 'l'), jsx('span', { children: bytes(tile.value) }, 'v')]
      }, tile.path || tile.name)
    })
  })
}

// ----- overview --------------------------------------------------------------------

function categoryValue(category) {
  if (category.action === 'command' && category.bytes === null) return 'Check'
  return bytes(category.bytes || 0)
}

function rankCategories(categories) {
  const weight = category => (category.priority === 'low' ? -1e18 : 0) + (category.action === 'projects' ? -1e17 : 0) + (category.status === 'attention' ? 1e18 : 0) + (category.reclaimable?.bytes || 0) * 2 + (category.bytes || 0)
  return [...(categories || [])].filter(category => category.status !== 'empty').sort((a, b) => weight(b) - weight(a))
}

function Footer({ data }) {
  const reclaimable = data?.reclaimable?.bytes
  const growth = data?.growth
  let growthText = 'growth baseline not yet established'
  if (growth?.status === 'comparable') growthText = `${signed(growth.deltaBytes)} since ${new Date(growth.since).toLocaleDateString()}`
  else if (growth?.status === 'unavailable') growthText = 'growth needs a complete scan'
  return jsxs('div', {
    className: 'su-footer',
    children: [
      `Verified reclaimable: ${reclaimable ? bytes(reclaimable) : 'unknown'} · ${growthText}`,
      growth?.status === 'comparable' && growth.changes?.length
        ? jsx('div', { className: 'su-meta', children: growth.changes.slice(0, 3).map(change => `${change.path} ${signed(change.deltaBytes)}`).join(' · ') })
        : null
    ]
  })
}

function Overview({ store, onCategory, onExplorer }) {
  const { data, overview } = store
  const [scanError, setScanError] = useState(null)
  if (overview.isError) return jsx('div', { className: 'su-error', children: errorText(overview.error) })
  if (!data) return jsxs(Fragment, { children: [jsx('div', { className: 'su-progress' }), jsx('p', { className: 'su-muted', children: 'Measuring this Hermes instance…' })] })
  const volume = data.capacity || {}
  const snapshot = data.snapshot
  const hermesBytes = snapshot?.totals?.allocatedBytes
  const used = volume.totalBytes != null && volume.freeBytes != null ? volume.totalBytes - volume.freeBytes : null
  const percent = used != null && volume.totalBytes ? Math.round((used / volume.totalBytes) * 100) : null
  const hermesShare = hermesBytes != null && volume.totalBytes ? Math.min(100, (hermesBytes / volume.totalBytes) * 100) : 0
  const otherShare = percent != null ? Math.max(0, percent - hermesShare) : 0
  const ranked = rankCategories(data.categories)
  const cards = ranked.slice(0, 4)
  const rows = ranked.slice(4)
  const scanning = Boolean(data.scan && !data.scan.finished)
  const coverage = snapshot?.coverage
  const firstAttention = ranked.find(category => category.status === 'attention') || ranked[0]
  const lowSpace = volume.availableBytes != null && volume.totalBytes && volume.availableBytes / volume.totalBytes < 0.1

  const rescan = async () => {
    setScanError(null)
    try { await store.startScan() } catch (error) { setScanError(errorText(error)) }
  }
  const stop = async () => {
    try { await store.post(`/scans/${encodeURIComponent(data.scan.scanId)}/cancel`); store.refresh() } catch (error) { setScanError(errorText(error)) }
  }

  return jsxs('div', {
    children: [
      jsxs('div', {
        className: 'su-capacity',
        children: [
          jsxs('div', { children: [jsx('strong', { children: `${bytes(volume.availableBytes)} free` }), jsx('div', { className: 'su-meta', children: `${bytes(used)} of ${bytes(volume.totalBytes)} used on this disk` })] }),
          jsx('span', { className: 'su-num', children: percent != null ? `${percent}%` : '' }),
          jsxs('div', { className: 'su-bar', 'aria-hidden': 'true', children: [jsx('i', { className: 'su-hermes', style: { width: `${hermesShare}%` } }), jsx('i', { className: 'su-other', style: { width: `${otherShare}%` } })] })
        ]
      }),
      jsx('p', { className: 'su-meta', children: 'Colour = Hermes. Grey = everything else on the disk, which Tune-up does not inventory.' }),
      lowSpace && jsx('div', { className: 'su-note su-warn', children: 'Less than 10% of this disk is free. Hermes can fail to save sessions when the disk fills up.' }),
      jsxs('div', {
        className: 'su-stats',
        children: [
          jsxs('div', { children: [jsx('strong', { children: bytes(hermesBytes) }), jsx('span', { className: 'su-meta', children: `Hermes footprint${snapshot ? (coverage?.complete ? '' : ' · partial') : ' · not scanned yet'}` })] }),
          jsxs('div', { children: [jsx('strong', { children: data.reclaimable?.bytes ? bytes(data.reclaimable.bytes) : '—' }), jsx('span', { className: 'su-meta', children: data.reclaimable?.bytes ? `Verified reclaimable · ${data.reclaimable.sources.map(source => source.title).join(', ')}` : 'Verified reclaimable: none measured' })] })
        ]
      }),
      jsxs('div', {
        className: 'su-scanline',
        children: [
          jsx('span', { children: scanning ? 'Scanning the Hermes home…' : snapshot ? `Scanned ${ago(snapshot.finishedAt)} · ${count(snapshot.totals?.files, 'file')}${coverage && !coverage.complete ? ` · stopped early (${coverage.budgetReason || 'limit'})` : ''}` : 'Not scanned yet' }),
          scanning ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: stop, children: 'Stop' }) : jsx(Button, { size: 'sm', variant: 'ghost', onClick: rescan, children: snapshot ? 'Rescan' : 'Scan' })
        ]
      }),
      scanning && jsx('div', { className: 'su-progress' }),
      scanError && jsx('div', { className: 'su-error', children: scanError }),
      data.command?.state === 'running' && jsx('div', { className: 'su-note', children: `Running ${data.command.label}…` }),
      jsx('div', {
        className: 'su-cards',
        children: cards.map(category => jsxs('button', {
          type: 'button',
          className: `su-card ${category.status === 'attention' ? 'su-attention' : ''}`,
          onClick: () => onCategory(category.id),
          children: [
            jsxs('span', { className: 'su-card-title', children: [icon(category.icon), category.title] }),
            jsx('strong', { children: categoryValue(category) }),
            jsx('span', { className: 'su-meta', children: category.scope === 'projects' ? `Outside ~/.hermes · ${category.summary}` : category.summary })
          ]
        }, category.id))
      }),
      rows.length > 0 && jsx('div', {
        className: 'su-rows',
        children: rows.map(category => jsxs('button', {
          type: 'button',
          className: 'su-row',
          onClick: () => onCategory(category.id),
          children: [icon(category.icon), jsxs('span', { children: [jsx('b', { children: category.title }), jsx('span', { className: 'su-meta', children: category.summary })] }), jsx('span', { className: 'su-num', children: categoryValue(category) })]
        }, category.id))
      }),
      jsxs('div', {
        className: 'su-actions',
        children: [
          onExplorer ? jsx('button', { type: 'button', className: 'su-link', onClick: onExplorer, children: 'Explore everything →' }) : jsx('span', {}),
          firstAttention ? jsx(Button, { size: 'sm', onClick: () => onCategory(firstAttention.id), children: 'Review cleanup' }) : null
        ]
      }),
      jsx(Footer, { data })
    ]
  })
}

// ----- friendly home ---------------------------------------------------------------

const TIER = {
  green: { dot: 'su-g', title: 'Safe to clean', sub: 'Hermes can rebuild or re-download these' },
  yellow: { dot: 'su-y', title: 'Take a quick look', sub: 'Probably not needed, but only you know' },
  pause: { dot: 'su-b', title: 'Squeeze out empty space (Hermes pauses briefly)', sub: 'Your chat history file keeps empty room after old chats are deleted. Packing removes that empty room; no conversations are deleted. Hermes has to stop for a few minutes while it runs.' },
  locked: { dot: 'su-k', title: 'Kept safe on purpose', sub: 'Still in use, so Tune-up won’t touch these' }
}

function detailsWanted() {
  try { return localStorage.getItem('storage-utility.details') === '1' } catch (_) { return false }
}

function Answers({ answers }) {
  return jsx('div', {
    className: 'su-qa',
    children: [['What is it?', answers.what], ['Is it safe?', answers.safe], ['If I remove it…', answers.removed], ['Can I undo?', answers.undo]]
      .map(([label, text]) => jsxs('div', { children: [jsx('span', { children: label }), text] }, label))
  })
}

function sizeText(row) {
  return row.sizeKnown ? bytes(row.bytes) : 'varies'
}

function FriendlyRow({ row, details, ticked, onTick, onOpen, extra }) {
  const [why, setWhy] = useState(false)
  const go = Boolean(onOpen)
  return jsxs('div', {
    className: `su-frow ${go ? 'su-go' : ''}`,
    onClick: go ? () => onOpen(row.id) : undefined,
    children: [
      onTick ? jsx('input', { type: 'checkbox', checked: ticked, onClick: event => event.stopPropagation(), onChange: () => onTick(row.id), 'aria-label': `Include ${row.title}` }) : jsx('span', {}),
      jsxs('div', {
        children: [
          jsx('b', { children: row.title }),
          jsx('p', { children: row.what }),
          row.projects?.length > 0 && jsx('p', { className: 'su-from', children: [
            row.projects.length === 1 && row.projects[0].name === 'Hermes\'s own folder' ? 'In Hermes’s own folder' : `From ${row.projects.length === 1 ? 'project' : count(row.projects.length, 'place')}: `,
            ...(row.projects.length === 1 && row.projects[0].name === 'Hermes\'s own folder' ? [] : row.projects.slice(0, 4).map((project, index) => jsxs('span', { children: [index ? ', ' : '', jsx('b', { children: project.name }), ` ${bytes(project.bytes)}`] }, project.name))),
            row.projects.length > 4 ? ` and ${row.projects.length - 4} more` : ''
          ] }),
          details && jsx('div', { className: 'su-tech', children: [row.technical?.title, ...(row.technical?.commands || [])].filter(Boolean).join(' · ') }),
          jsx('button', { type: 'button', className: 'su-why', onClick: event => { event.stopPropagation(); setWhy(value => !value) }, children: why ? 'Hide' : (row.tier === 'green' ? 'Why is this safe?' : 'What is this?') }),
          why && jsx(Answers, { answers: row.answers }),
          extra
        ]
      }),
      jsx('span', { className: 'su-num', children: go ? `${sizeText(row)} ›` : sizeText(row) })
    ]
  })
}

function Group({ tier, total, open, onToggle, children }) {
  const meta = TIER[tier]
  return jsxs('div', {
    className: `su-group ${open ? 'su-open' : ''}`,
    children: [
      jsxs('button', {
        type: 'button', className: 'su-ghead', 'aria-expanded': open ? 'true' : 'false', onClick: onToggle,
        children: [jsx('span', { className: `su-dot ${meta.dot}` }), jsxs('span', { children: [jsx('b', { children: meta.title }), jsx('span', { className: 'su-meta', children: meta.sub })] }), jsx('strong', { children: total }), jsx('span', { className: 'su-chev', children: '›' })]
      }),
      open && jsx('div', { className: 'su-gbody', children })
    ]
  })
}

const PACK_DONE = { busy: 'Not packed: the nightly backup or an update was running', done: 'Packed', blocked: 'Not packed: something else was using the file', 'no-space': 'Not packed: not enough free disk space', failed: 'Not packed: Hermes’s packing command failed', interrupted: 'Stopped part-way; the file was left as it was', 'check-failed': 'Packed, but the check afterwards found a difference', nothing: 'Nothing to pack' }

function packResult(last) {
  if (!last || !last.state || last.state === 'running') return null
  const restartFailed = last.state.endsWith('-restart-failed')
  const base = PACK_DONE[last.state.replace(/-restart-failed$/, '')] || last.state
  const saved = last.state === 'done' && last.savedBytes ? ` — got back ${bytes(last.savedBytes)}` : ''
  const when = last.finishedAt ? ` (${ago(last.finishedAt)})` : ''
  return { ok: last.state === 'done', text: `Last time: ${base}${saved}${when}.${restartFailed ? ` Some services did not start again: ${(last.restartFailed || []).join(', ')}. Start them with systemctl start.` : ''}`, why: last.state !== 'done' ? last.phase : null }
}

function PackNow({ store, last, onSteps }) {
  const [plan, setPlan] = useState(null)
  const [accepted, setAccepted] = useState(false)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState(null)
  const [started, setStarted] = useState(null)
  const result = packResult(last)
  const review = async () => {
    setWorking(true); setError(null); setAccepted(false)
    try { setPlan(await rest('/compact', { timeoutMs: 120000 })) } catch (failure) { setError(errorText(failure)) } finally { setWorking(false) }
  }
  const start = async () => {
    setWorking(true); setError(null)
    try { setStarted(await store.post('/compact/start', { units: plan.stopOrder, confirmation: { acknowledged: true } })); setPlan(null) } catch (failure) { setError(errorText(failure)) } finally { setWorking(false) }
  }
  if (started || last?.state === 'running') {
    return jsx('div', { className: 'su-note', style: { marginTop: 8 }, children: `Packing now. Hermes is pausing, so this screen will disconnect for about ${started?.estimateMinutes || 'a few'} minute${started?.estimateMinutes === 1 ? '' : 's'} and come back by itself. Your chats are safe.` })
  }
  return jsxs('div', {
    style: { marginTop: 8 },
    children: [
      result && jsx('div', { className: `su-note ${result.ok ? 'su-ok' : 'su-warn'}`, children: [result.text, result.why ? jsx('div', { className: 'su-meta', children: result.why }) : null] }),
      !plan && jsxs('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 6 }, children: [
        jsx(Button, { size: 'sm', disabled: working, onClick: review, children: working ? 'Checking…' : 'Pack it now…' }),
        jsx(Button, { size: 'sm', variant: 'ghost', onClick: onSteps, children: 'Show me the steps' })
      ] }),
      plan && !plan.available && jsxs('div', { className: 'su-note su-warn', children: [plan.reason, ' ', jsx('button', { type: 'button', className: 'su-link', onClick: () => setPlan(null), children: 'OK' })] }),
      plan && plan.available && jsxs('div', { className: 'su-cmd', children: [
        jsx('b', { children: `Hermes will be offline for about ${plan.estimateMinutes} minute${plan.estimateMinutes === 1 ? '' : 's'}` }),
        jsx('p', { className: 'su-muted', children: 'These pause, then start again by themselves:' }),
        jsx('ul', { style: { margin: '4px 0 6px 18px', padding: 0, fontSize: 12 }, children: plan.pauses.map(item => jsx('li', { children: item.label }, item.unit)) }),
        jsx('p', { className: 'su-meta', children: 'Any chat or agent that is busy right now gets interrupted. Nothing is deleted. If anything goes wrong, Hermes is started again and the file is left as it was.' }),
        jsxs('label', { className: 'su-check', children: [jsx('input', { type: 'checkbox', checked: accepted, onChange: event => setAccepted(event.target.checked) }), jsx('span', { children: 'OK to pause Hermes now' })] }),
        jsxs('div', { className: 'su-actions su-end', children: [
          jsx(Button, { size: 'sm', variant: 'ghost', disabled: working, onClick: () => setPlan(null), children: 'Cancel' }),
          jsx(Button, { size: 'sm', disabled: !accepted || working, onClick: start, children: working ? 'Starting…' : 'Pause Hermes and pack' })
        ] })
      ] }),
      error && jsx('div', { className: 'su-error', children: error })
    ]
  })
}

// ----- health checkup -----------------------------------------------------------------

function forecastLine(value) {
  if (!value) return null
  if (value.status === 'growing') {
    const days = value.days
    const when = days <= 1 ? 'within a day' : days < 14 ? `in about ${days} days` : days < 60 ? `in about ${Math.round(days / 7)} weeks` : days < 365 ? `in about ${Math.round(days / 30)} months` : null
    return when ? `At this rate the drive is full ${when} (growing about ${bytes(value.perDay)} a day).` : `Growing about ${bytes(value.perDay)} a day; plenty of time.`
  }
  if (value.status === 'steady') return 'Drive use is steady.'
  return null
}

function useCheckup(store, enabled = true) {
  return useQuery({
    queryKey: [ID, 'checkup', store.scopeKey],
    queryFn: () => rest('/checkup', { timeoutMs: 120000 }),
    enabled: Boolean(store.binding) && enabled,
    staleTime: 5 * 60000,
    retry: false,
    refetchOnWindowFocus: false
  })
}

function HealthBanner({ store, onCheckup }) {
  const checkup = useCheckup(store)
  const data = checkup.data
  if (!data || !data.available) return null
  const bad = data.cards.filter(card => card.level === 'bad').length
  const warn = data.cards.filter(card => card.level === 'warn').length
  if (!bad && !warn) return null
  return jsxs('button', {
    type: 'button', className: `su-note ${bad ? 'su-warn' : ''}`, onClick: onCheckup,
    style: { all: 'unset', boxSizing: 'border-box', display: 'block', width: '100%', cursor: 'pointer', margin: '0 0 10px', padding: '8px 10px', borderRadius: 7, fontSize: 12, background: bad ? 'color-mix(in srgb,var(--ui-danger,#f85149) 14%,transparent)' : 'color-mix(in srgb,var(--ui-warning,#d29922) 13%,transparent)' },
    children: [jsx('b', { children: bad ? `${count(bad, 'health problem')} need attention` : `${count(warn, 'thing')} worth a look` }), ' · Open Checkup ›']
  })
}

function FixCard({ store, card, onDone }) {
  const [open, setOpen] = useState(false)
  const [accepted, setAccepted] = useState(false)
  const [picked, setPicked] = useState(() => new Set())
  const [working, setWorking] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const items = card.items || []
  const fix = card.fix
  const run = async () => {
    setWorking(true); setError(null)
    try {
      const outcome = await store.post('/checkup/fix', { action: fix.action, unit: fix.unit, ids: [...picked], confirmation: { acknowledged: true } }, { timeoutMs: 300000 })
      setResult(outcome); setOpen(false); setAccepted(false); setPicked(new Set()); onDone(outcome)
    } catch (failure) { setError(errorText(failure)) } finally { setWorking(false) }
  }
  const tone = card.level === 'bad' ? 'su-warn' : card.level === 'info' ? '' : 'su-warn'
  return jsxs('div', {
    className: 'su-cmd',
    style: card.level === 'bad' ? { borderColor: 'color-mix(in srgb,var(--ui-danger,#f85149) 55%,var(--ui-stroke-secondary))' } : undefined,
    children: [
      jsxs('div', { style: { display: 'flex', gap: 8, alignItems: 'baseline' }, children: [jsx('span', { className: `su-dot ${card.level === 'bad' ? 'su-bad' : card.level === 'warn' ? 'su-y' : 'su-b'}` }), jsx('b', { children: card.title })] }),
      jsx('p', { className: 'su-muted', children: card.text }),
      items.length > 0 && jsx('div', { className: 'su-list', style: { margin: '6px 0' }, children: items.map(item => jsxs('label', {
        className: 'su-item',
        children: [
          jsx('input', { type: 'checkbox', checked: picked.has(item.id), onChange: () => setPicked(previous => toggleIn(previous, item.id)), 'aria-label': `Stop ${item.command}` }),
          jsxs('span', { children: [
            jsx('span', { className: 'su-path', style: { whiteSpace: 'normal' }, title: item.command, children: item.what || (item.description && item.kind === 'unit' ? item.description : item.command) }),
            jsx(ChatLine, { chat: item.chat }),
            jsxs('span', { className: 'su-advice', style: { display: 'block' }, children: [jsx('b', { style: { color: item.verdict === 'check-first' ? 'var(--ui-warning,#d29922)' : 'var(--ui-success,#3fb950)' }, children: item.verdict === 'check-first' ? 'Check first. ' : 'Probably not needed. ' }), item.advice.replace(/^(Probably not needed|Not needed|Check first)[.:]?\s*/, '')] }),
            jsx('span', { className: 'su-meta', style: { whiteSpace: 'normal' }, children: [`running for ${item.ageText}`, item.ports?.length ? `port ${item.ports.join(', ')}` : '', item.description && item.kind === 'unit' ? item.description : ''].filter(Boolean).join(' · ') }),
            jsx('details', { className: 'su-meta', children: [jsx('summary', { style: { cursor: 'pointer' }, children: 'Technical details' }), jsx('code', { style: { wordBreak: 'break-all', fontSize: 11 }, children: item.command })] })
          ] }),
          jsx('span', { className: 'su-num', children: item.memoryBytes ? bytes(item.memoryBytes) : item.ageText })
        ]
      }, item.id)) }),
      result && jsx('div', { className: `su-note ${result.ok ? 'su-ok' : 'su-warn'}`, children: result.done.join(' · ') }),
      error && jsx('div', { className: 'su-error', children: error }),
      fix && !open && jsx('div', { className: 'su-actions su-end', children: jsx(Button, { size: 'sm', variant: card.level === 'info' ? 'outline' : 'default', disabled: items.length > 0 && !picked.size, onClick: () => setOpen(true), children: items.length ? `${fix.label.replace('…', '')} ${picked.size || ''}`.trim() : fix.label }) }),
      fix && open && jsxs('div', { children: [
        jsxs('label', { className: 'su-check', children: [jsx('input', { type: 'checkbox', checked: accepted, onChange: event => setAccepted(event.target.checked) }), jsx('span', { children: items.length ? `Stop exactly these ${count(picked.size, 'thing')}. Anything that changed since this list was made is skipped.` : `OK: ${fix.label.toLowerCase()}` })] }),
        jsxs('div', { className: 'su-actions su-end', children: [
          jsx(Button, { size: 'sm', variant: 'ghost', disabled: working, onClick: () => setOpen(false), children: 'Cancel' }),
          jsx(Button, { size: 'sm', disabled: !accepted || working, onClick: run, children: working ? 'Working…' : 'Do it' })
        ] })
      ] })
    ]
  })
}

function CheckupView({ store }) {
  const checkup = useCheckup(store)
  const queryClient = useQueryClient()
  const data = checkup.data
  if (checkup.isError) return jsx('div', { className: 'su-error', children: errorText(checkup.error) })
  if (!data) return jsxs(Fragment, { children: [jsx('div', { className: 'su-progress' }), jsx('p', { className: 'su-muted', children: 'Checking Hermes’s health…' })] })
  if (!data.available) return jsx('p', { className: 'su-muted', children: data.reason })
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: [ID, 'checkup'] }) }
  const bad = data.cards.filter(card => card.level === 'bad').length
  return jsxs('div', {
    children: [
      jsx('div', { className: `su-note ${bad ? 'su-warn' : 'su-ok'}`, children: bad ? `${count(bad, 'problem')} need attention.` : 'Hermes is healthy: everything that should be running is running.' }),
      jsx('h4', { children: 'Hermes services' }),
      jsx('div', { className: 'su-list', children: data.services.map(service => jsxs('div', { className: 'su-item', children: [
        jsx('span', { className: `su-dot ${service.active === 'active' ? 'su-g' : 'su-bad'}` }),
        jsxs('span', { children: [jsx('span', { className: 'su-path', children: service.label.charAt(0).toUpperCase() + service.label.slice(1) }), jsx('span', { className: 'su-meta', children: `${service.active === 'active' ? 'running' : service.active}${service.restart === 'always' ? ' · restarts by itself' : ''}${service.restarts ? ` · restarted ${service.restarts}×` : ''}` })] }),
        jsx('span', { className: 'su-num', children: '' })
      ] }, service.unit)) }),
      data.tunnels?.length > 0 && jsx('p', { className: 'su-meta', style: { marginTop: 6 }, children: `Reachable from outside through: ${data.tunnels.map(name => name.startsWith('cloudflared') ? 'Cloudflare tunnel' : name.startsWith('tailscale') ? 'Tailscale' : name).join(', ')} (running).` }),
      data.cards.length > 0 && jsx('h4', { children: 'Things to look at' }),
      ...data.cards.map(card => jsx(FixCard, { store, card, onDone: refresh }, card.id)),
      jsx('div', { className: 'su-actions su-end', children: jsx(Button, { size: 'sm', variant: 'ghost', disabled: checkup.isFetching, onClick: () => void checkup.refetch(), children: checkup.isFetching ? 'Checking…' : 'Check again' }) })
    ]
  })
}

function FreeUp({ store, plan, onCancel, onDone }) {
  const [working, setWorking] = useState(false)
  const [error, setError] = useState(null)
  const apply = async () => {
    setWorking(true); setError(null)
    try {
      const result = await store.post('/freeup/apply', {
        plans: plan.plans.map(row => ({ planId: row.planId, digest: row.digest })),
        commands: plan.commands.map(row => ({ commandId: row.commandId, params: row.params || {} })),
        idempotencyKey: plan.plans.map(row => row.planId).join('.').slice(0, 90) || `cmd-${Date.now()}`,
        confirmation: { acknowledged: true }
      }, { timeoutMs: 600000 })
      onDone(result)
    } catch (failure) { setError(errorText(failure)) } finally { setWorking(false) }
  }
  return jsxs('div', {
    children: [
      jsx('h3', { style: { fontSize: 16 }, children: plan.totalBytes ? `About to clean up ${bytes(plan.totalBytes)}` : 'About to clean up' }),
      jsx('p', { className: 'su-muted', children: 'These go:' }),
      jsx('div', {
        className: 'su-list', style: { margin: '6px 0' },
        children: [
          ...plan.plans.map(row => jsxs('div', { className: 'su-item', children: [jsx('span', { className: 'su-dot su-g' }), jsxs('span', { children: [jsx('b', { style: { fontWeight: 600 }, children: row.title }), jsx('span', { className: 'su-meta', style: { display: 'block', whiteSpace: 'normal' }, children: `${count(row.count, 'item')} · e.g. ${row.examples.slice(0, 2).join(', ')}` })] }), jsx('span', { className: 'su-num', children: bytes(row.totalBytes) })] }, row.planId)),
          ...plan.commands.map(row => jsxs('div', { className: 'su-item', children: [jsx('span', { className: 'su-dot su-g' }), jsxs('span', { children: [jsx('b', { style: { fontWeight: 600 }, children: row.title }), jsx('span', { className: 'su-meta', style: { display: 'block' }, children: row.commandId === 'git-gc' ? 'Packed tighter; nothing in the project changes' : 'Only what nothing uses is removed' })] }), jsx('span', { className: 'su-num', children: 'varies' })] }, row.commandId + JSON.stringify(row.params || {})))
        ]
      }),
      jsx('div', { className: 'su-note', children: 'These are all things Hermes can rebuild or download again, or extra copies where one copy stays. So they’re deleted right away and you get the space back immediately. Anything that changed since the check is skipped.' }),
      error && jsx('div', { className: 'su-error', children: error }),
      jsx('button', { type: 'button', className: 'su-big', disabled: working, onClick: apply, children: working ? 'Cleaning up…' : 'Yes, clean up' }),
      jsx('div', { className: 'su-actions su-end', children: jsx(Button, { size: 'sm', variant: 'ghost', disabled: working, onClick: onCancel, children: 'Cancel' }) })
    ]
  })
}

function FreeUpDone({ store, result, yellowTotal, onBack }) {
  const jobId = result.commandJob?.jobId
  const job = useQuery({
    queryKey: [ID, 'job', store.scopeKey, jobId],
    queryFn: () => rest(`/commands/jobs/${encodeURIComponent(jobId)}`),
    enabled: Boolean(jobId),
    refetchInterval: query => (!query.state.data || query.state.data.state === 'running' ? 1500 : false),
    retry: false
  })
  const tidying = Boolean(jobId) && (!job.data || job.data.state === 'running')
  const measured = result.volumeFreeDelta != null && result.volumeFreeDelta > 0 ? result.volumeFreeDelta : result.bytesHandled
  const extra = job.data?.volumeFreeDelta > 0 ? job.data.volumeFreeDelta : 0
  const freeNow = store.data?.capacity?.availableBytes ?? result.freeAfter
  return jsxs('div', {
    className: 'su-done',
    children: [
      jsx('div', { className: 'su-tick', children: '✓' }),
      jsx('h3', { children: `Done — you got back ${bytes(measured + extra)}` }),
      freeNow != null && jsx('p', { className: 'su-muted', children: `Your drive now has ${bytes(freeNow)} free.` }),
      tidying && jsx('div', { className: 'su-note', children: 'Still tidying Hermes’s own downloads. This can take a minute; you can close this.' }),
      result.refusedCount > 0 && jsx('p', { className: 'su-meta', children: `${count(result.refusedCount, 'item')} changed since the check and ${result.refusedCount === 1 ? 'was' : 'were'} left alone.` }),
      yellowTotal > 0 && jsx('div', { className: 'su-note', style: { textAlign: 'left' }, children: `Want more? “Take a quick look” has ${bytes(yellowTotal)} of old backups and projects. Anything you remove there can be brought back for 30 days.` }),
      jsx(Button, { size: 'sm', variant: 'outline', onClick: onBack, children: 'Back to Tune-up' })
    ]
  })
}

function Home({ store, onCategory, onExplorer, onCheckup }) {
  const { data, overview } = store
  const [details, setDetails] = useState(detailsWanted)
  const [unticked, setUnticked] = useState(() => new Set())
  const [open, setOpen] = useState(() => new Set())
  const [plan, setPlan] = useState(null)
  const [result, setResult] = useState(null)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState(null)
  if (overview.isError) return jsx('div', { className: 'su-error', children: errorText(overview.error) })
  if (!data) return jsxs(Fragment, { children: [jsx('div', { className: 'su-progress' }), jsx('p', { className: 'su-muted', children: 'Having a look at your drive…' })] })
  if (!data.home) return jsx('div', { className: 'su-note su-warn', children: 'This screen is newer than Tune-up on the Hermes computer, so it can’t show the cleanup groups yet. See the note above to finish updating.' })
  const home = data.home
  const { green, yellow, pause } = home.tiers
  const totals = home.totals || {}
  const yellowTotal = totals.yellow || 0
  if (result) return jsx(FreeUpDone, { store, result, yellowTotal, onBack: () => { setResult(null); store.refresh() } })
  if (plan) return jsx(FreeUp, { store, plan, onCancel: () => setPlan(null), onDone: outcome => { setPlan(null); setResult(outcome); store.refresh() } })

  const volume = data.capacity || {}
  const total = volume.totalBytes || 0
  const free = volume.availableBytes
  const share = value => (total ? `${Math.max(0, Math.min(100, (value / total) * 100))}%` : '0%')
  const ratio = total && free != null ? free / total : null
  const word = ratio == null ? '' : ratio < 0.1 ? 'almost full' : ratio < 0.25 ? 'getting full' : 'plenty of room'
  const chosen = green.filter(row => !unticked.has(row.id))
  const chosenBytes = chosen.reduce((sum, row) => sum + (row.bytes || 0), 0)
  const greenTotal = totals.green || 0
  const pauseTotal = totals.pause || 0
  const usedOther = Math.max(0, total - (free || 0) - greenTotal - yellowTotal - pauseTotal)
  const scanning = Boolean(data.scan && !data.scan.finished)
  const toggle = tier => setOpen(previous => toggleIn(previous, tier))
  const setDetail = value => { setDetails(value); try { localStorage.setItem('storage-utility.details', value ? '1' : '0') } catch (_) {} }

  const start = async () => {
    setWorking(true); setError(null)
    try { setPlan(await store.post('/freeup/plan', { categoryIds: chosen.map(row => row.id) }, { timeoutMs: 180000 })) } catch (failure) { setError(errorText(failure)) } finally { setWorking(false) }
  }

  return jsxs('div', {
    children: [
      jsxs('div', { className: 'su-head', children: [`${bytes(free)} free of ${bytes(total)}`, word && jsx('span', { className: ratio < 0.25 ? 'su-word-low' : 'su-word-ok', children: ` · ${word}` })] }),
      forecastLine(home.forecast) && jsx('p', { className: 'su-meta', style: { margin: '-4px 0 8px' }, children: forecastLine(home.forecast) }),
      jsx(HealthBanner, { store, onCheckup }),
      jsxs('div', { className: 'su-tbar', 'aria-hidden': 'true', children: [jsx('i', { className: 'su-g', style: { width: share(greenTotal) } }), jsx('i', { className: 'su-y', style: { width: share(yellowTotal) } }), jsx('i', { className: 'su-b', style: { width: share(pauseTotal) } }), jsx('i', { className: 'su-o', style: { width: share(usedOther) } })] }),
      jsxs('div', { className: 'su-legend', children: [
        jsxs('span', { children: [jsx('i', { className: 'su-dot su-g' }), `safe ${greenTotal ? bytes(greenTotal) : 'varies'}`] }),
        jsxs('span', { children: [jsx('i', { className: 'su-dot su-y' }), `look ${bytes(yellowTotal)}`] }),
        pauseTotal > 0 && jsxs('span', { children: [jsx('i', { className: 'su-dot su-b' }), `pause ${bytes(pauseTotal)}`] }),
        jsxs('span', { children: [jsx('i', { className: 'su-dot su-o' }), 'everything else'] })
      ] }),
      ...(home.notices || []).map((notice, index) => jsx('div', { className: 'su-note su-warn', children: notice }, `notice-${index}`)),
      home.empty
        ? jsx('div', { className: 'su-note su-ok', children: scanning ? 'Having a look…' : 'Nothing to clean up right now. Hermes is tidy. Tune-up will keep an eye on it and show you here when there is something worth clearing.' })
        : greenTotal > 0 || green.length
          ? jsxs('p', { className: 'su-say', children: ['Hermes can safely give back ', jsx('b', { children: greenTotal ? `about ${bytes(greenTotal)}` : 'some space' }), ' right now.'] })
          : jsx('p', { className: 'su-say', children: 'Nothing is safe to clean automatically right now. Have a look at the groups below.' }),
      green.length > 0 && jsx('button', { type: 'button', className: 'su-big', disabled: working || !chosen.length, onClick: start, children: working ? 'Getting the list ready…' : chosenBytes ? `Free up ${bytes(chosenBytes)} safely` : 'Free up space safely' }),
      green.length > 0 && jsx('div', { className: 'su-sub', children: 'Only things Hermes can rebuild or download again. You’ll see the list first.' }),
      error && jsx('div', { className: 'su-error', children: error }),
      scanning && jsx('div', { className: 'su-progress' }),
      data.projects?.running && jsx('p', { className: 'su-meta', children: 'Still checking folders Hermes made elsewhere on this computer…' }),
      green.length > 0 && jsx(Group, { tier: 'green', total: greenTotal ? bytes(greenTotal) : 'varies', open: open.has('green'), onToggle: () => toggle('green'), children: green.map(row => jsx(FriendlyRow, { row, details, ticked: !unticked.has(row.id), onTick: id => setUnticked(previous => toggleIn(previous, id)) }, row.id)) }),
      yellow.length > 0 && jsx(Group, { tier: 'yellow', total: bytes(yellowTotal), open: open.has('yellow'), onToggle: () => toggle('yellow'), children: [
        jsx('p', { className: 'su-meta', style: { margin: '6px 0 2px' }, children: 'Tap one to see what’s inside. Anything you remove can be brought back for 30 days.' }, 'hint'),
        ...yellow.map(row => jsx(FriendlyRow, { row, details, onOpen: onCategory }, row.id))
      ] }),
      pause.length > 0 && jsx(Group, { tier: 'pause', total: bytes(pauseTotal), open: open.has('pause'), onToggle: () => toggle('pause'), children: pause.map(row => jsx(FriendlyRow, {
        row, details,
        extra: jsx(PackNow, { store, last: home.compact, onSteps: () => onCategory(row.id) })
      }, row.id)) }),
      home.locked?.count > 0 && jsx(Group, { tier: 'locked', total: bytes(home.locked.bytes), open: open.has('locked'), onToggle: () => toggle('locked'), children: home.locked.items.map((item, index) => jsxs('div', {
        className: 'su-frow',
        children: [jsx('span', { children: '🔒' }), jsxs('div', { children: [jsx('b', { style: { overflowWrap: 'anywhere' }, children: item.name.split('/').pop() || item.name }), jsx('p', { className: 'su-lock', children: item.project && item.project !== 'Hermes\'s own folder' ? `${item.project} · ${item.why}` : item.why }), details && jsx('div', { className: 'su-tech', children: `${item.name}${item.detail ? ` · ${item.detail}` : ''}` })] }), jsx('span', { className: 'su-num', children: bytes(item.bytes) })]
      }, index)) }),
      jsxs('div', { className: 'su-scanline', style: { marginTop: 8 }, children: [
        jsx('span', { children: scanning ? 'Checking your drive…' : data.snapshot ? `Checked ${ago(data.snapshot.finishedAt)}` : 'Not checked yet' }),
        !scanning && jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => void store.startScan().catch(() => {}), children: 'Check again' })
      ] }),
      jsxs('label', { className: 'su-switch', children: [jsx('span', { children: 'Show details (file locations, commands)' }), jsx('input', { type: 'checkbox', checked: details, onChange: event => setDetail(event.target.checked) })] }),
      details && jsxs('div', { style: { marginTop: 10 }, children: [jsx('h4', { children: 'Technical view' }), jsx(Overview, { store, onCategory, onExplorer })] })
    ]
  })
}

// ----- category detail -------------------------------------------------------------

// Items from folders Hermes made elsewhere are shown under their project name, biggest project first.
function shortDate(seconds) {
  if (!seconds) return ''
  const date = new Date(seconds * 1000)
  const sameYear = date.getFullYear() === new Date().getFullYear()
  return date.toLocaleDateString(undefined, sameYear ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' })
}

function ChatLine({ chat }) {
  if (!chat || !chat.title) return null
  return jsxs('span', { className: 'su-chat', children: [
    'From the chat ', jsx('b', { children: `“${chat.title}”` }), chat.startedAt ? ` · ${shortDate(chat.startedAt)}` : '', chat.app ? ` (${chat.app})` : '',
    chat.about && chat.about !== chat.title ? jsx('span', { className: 'su-meta', style: { display: 'block', whiteSpace: 'normal' }, children: `You asked: “${chat.about}”` }) : null
  ] })
}

function groupedByProject(items) {
  if (!items.some(item => item.project)) {
    // Hermes's own files: group by the chat they came from, so people see what each was about.
    if (!items.some(item => item.chat?.title)) return [[null, items]]
    const groups = new Map()
    for (const item of items) {
      const key = item.chat?.title ? `Chat: “${item.chat.title}”` : 'Not linked to a chat'
      if (!groups.has(key)) groups.set(key, [])
      groups.get(key).push(item)
    }
    const size = rows => rows.reduce((sum, item) => sum + item.allocatedBytes, 0)
    return [...groups.entries()].sort((left, right) => (left[0] === 'Not linked to a chat') - (right[0] === 'Not linked to a chat') || size(right[1]) - size(left[1]))
  }
  const groups = new Map()
  for (const item of items) {
    const key = item.project || 'Hermes’s own folder'
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(item)
  }
  const total = rows => rows.reduce((sum, item) => sum + item.allocatedBytes, 0)
  return [...groups.entries()].sort((left, right) => total(right[1]) - total(left[1]))
}

function ReviewItems({ store, category, trash, onDone }) {
  const [selected, setSelected] = useState(() => new Set())
  const [operation, setOperation] = useState(trash?.available ? 'trash' : 'permanent')
  const [plan, setPlan] = useState(null)
  const [accepted, setAccepted] = useState(false)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const items = category.items || []
  const eligible = items.filter(item => item.selectable)
  const chosen = items.filter(item => selected.has(item.itemId))

  const review = async () => {
    setWorking(true); setError(null); setAccepted(false)
    try {
      setPlan(await store.post('/plans', { categoryId: category.id, itemIds: [...selected], operation }, { timeoutMs: 120000 }))
    } catch (failure) { setError(errorText(failure)) } finally { setWorking(false) }
  }
  const apply = async () => {
    setWorking(true); setError(null)
    try {
      const outcome = await store.post(`/plans/${encodeURIComponent(plan.planId)}/apply`, {
        digest: plan.digest,
        idempotencyKey: `${plan.planId}:${plan.digest.slice(0, 12)}`,
        confirmation: { operation: plan.operation, acknowledged: true }
      }, { timeoutMs: 300000 })
      setResult(outcome); setPlan(null); setSelected(new Set()); onDone()
    } catch (failure) { setError(errorText(failure)) } finally { setWorking(false) }
  }

  if (result) {
    const refused = result.outcomes.filter(row => row.status !== 'applied')
    return jsxs('div', {
      children: [
        jsx('div', {
          className: `su-note ${result.appliedCount ? 'su-ok' : 'su-warn'}`,
          children: result.operation === 'trash'
            ? `Removed ${count(result.appliedCount, 'item')} (${bytes(result.bytesHandled)}). You can bring ${result.appliedCount === 1 ? 'it' : 'them'} back from the Trash tab for 30 days; after that the space is given back automatically. Need the space now? Empty ${result.appliedCount === 1 ? 'it' : 'them'} from the Trash tab.`
            : `Deleted ${count(result.appliedCount, 'item')} (${bytes(result.bytesHandled)}). Free space on the disk changed by ${signed(result.volumeFreeDelta)}.`
        }),
        refused.length > 0 && jsxs('div', { className: 'su-note su-warn', children: [`${count(refused.length, 'item')} left untouched:`, ...refused.map(row => jsx('div', { className: 'su-meta', children: `${row.path} — ${row.reason}` }, row.itemId))] }),
        jsx(Button, { size: 'sm', variant: 'outline', onClick: () => setResult(null), children: 'Done' })
      ]
    })
  }

  if (plan) {
    return jsxs('div', {
      children: [
        jsx('h3', { children: plan.operation === 'permanent' ? `Delete ${count(plan.items.length, 'item')} for good?` : `Remove ${count(plan.items.length, 'item')}? (you can undo for 30 days)` }),
        jsx('div', {
          className: 'su-list',
          style: { margin: '8px 0' },
          children: plan.items.map(item => jsxs('div', {
            className: 'su-item',
            children: [
              icon(item.kind === 'dir' ? 'folder' : 'file'),
              jsxs('span', { children: [jsx('span', { className: 'su-path', title: item.path, children: item.path }), item.files > 1 ? jsx('span', { className: 'su-meta', children: count(item.files, 'file') }) : null] }),
              jsx('span', { className: 'su-num', children: bytes(item.allocatedBytes) })
            ]
          }, item.itemId))
        }),
        jsx('div', { className: `su-note ${plan.operation === 'permanent' ? 'su-warn' : ''}`, children: `${plan.consequence} ${plan.recovery}` }),
        jsxs('label', {
          className: 'su-check',
          children: [
            jsx('input', { type: 'checkbox', checked: accepted, onChange: event => setAccepted(event.target.checked) }),
            jsx('span', { children: plan.operation === 'permanent' ? `I understand these ${bytes(plan.totalBytes)} are deleted for good.` : 'Yes, remove exactly these.' })
          ]
        }),
        jsx('p', { className: 'su-meta', children: `Anything that changes before you confirm is skipped automatically. This review expires at ${new Date(plan.expiresAt).toLocaleTimeString()}.` }),
        error && jsx('div', { className: 'su-error', children: error }),
        jsxs('div', {
          className: 'su-actions',
          children: [
            jsx(Button, { size: 'sm', variant: 'ghost', disabled: working, onClick: () => setPlan(null), children: 'Back' }),
            jsx(Button, { size: 'sm', variant: plan.operation === 'permanent' ? 'destructive' : 'default', disabled: !accepted || working, onClick: apply, children: working ? 'Working…' : plan.operation === 'permanent' ? 'Delete for good' : 'Remove' })
          ]
        })
      ]
    })
  }

  if (!items.length) return jsx('p', { className: 'su-muted', children: 'Nothing here right now.' })
  return jsxs('div', {
    children: [
      jsxs('div', {
        className: 'su-scanline',
        children: [
          jsx('span', { children: `${count(category.itemTotal ?? items.length, 'item')} · ${eligible.length} can be removed${(category.itemTotal || 0) > items.length ? ` · largest ${items.length} shown` : ''}` }),
          eligible.length > 0 && jsx('button', { type: 'button', className: 'su-link', onClick: () => setSelected(selected.size === eligible.length ? new Set() : new Set(eligible.map(item => item.itemId))), children: selected.size === eligible.length ? 'Clear' : 'Select all' })
        ]
      }),
      jsx('div', {
        className: 'su-list',
        children: groupedByProject(items).flatMap(([project, rows]) => [
          project ? jsxs('div', { className: 'su-projhead', style: { flexWrap: 'wrap' }, children: [icon(project.startsWith('Chat:') ? 'comment-discussion' : 'folder'), jsx('b', { style: { flex: 1, minWidth: 0 }, children: project }), jsx('span', { className: 'su-num', children: bytes(rows.reduce((sum, item) => sum + item.allocatedBytes, 0)) }),
            project.startsWith('Chat:') && rows[0].chat ? jsx('span', { className: 'su-meta', style: { flexBasis: '100%', whiteSpace: 'normal' }, children: [rows[0].chat.startedAt ? shortDate(rows[0].chat.startedAt) : '', rows[0].chat.app ? `in the ${rows[0].chat.app}` : '', rows[0].chat.about && rows[0].chat.about !== rows[0].chat.title ? `You asked: “${rows[0].chat.about}”` : ''].filter(Boolean).join(' · ') }) : null] }, `head-${project}`) : null,
          ...rows.map(item => jsxs('label', {
          className: `su-item ${item.selectable ? '' : 'su-disabled'}`,
          children: [
            jsx('input', { type: 'checkbox', disabled: !item.selectable, checked: selected.has(item.itemId), onChange: () => setSelected(previous => toggleIn(previous, item.itemId)), 'aria-label': `Select ${item.display || item.path}` }),
            jsxs('span', { children: [
              jsx('span', { className: 'su-path', title: item.display || item.path, children: item.display ? item.display.replace(/^\/root\//, '~/') : item.path }),
              item.what && jsx('span', { className: 'su-chat', children: item.what }),
              !item.project && item.chat?.title ? null : jsx(ChatLine, { chat: item.chat }),
              jsx('span', { className: 'su-meta', style: { whiteSpace: 'normal' }, children: [item.kind === 'dir' ? count(item.files, 'file') : 'file', `last changed ${item.ageDays} days ago`, item.note].filter(Boolean).join(' · ') })
            ] }),
            jsx('span', { className: 'su-num', children: bytes(item.allocatedBytes) })
          ]
        }, item.itemId))
        ])
      }),
      jsxs('div', {
        className: 'su-radio',
        role: 'radiogroup',
        children: [
          jsxs('label', { children: [jsx('input', { type: 'radio', name: `op-${category.id}`, checked: operation === 'trash', disabled: !trash?.available, onChange: () => setOperation('trash') }), 'Remove (you can undo this for 30 days)'] }),
          jsxs('label', { children: [jsx('input', { type: 'radio', name: `op-${category.id}`, checked: operation === 'permanent', onChange: () => setOperation('permanent') }), 'Delete for good (space back right away)'] })
        ]
      }),
      trash && !trash.available && jsx('p', { className: 'su-meta', children: `Trash unavailable: ${trash.reason}` }),
      error && jsx('div', { className: 'su-error', children: error }),
      jsxs('div', {
        className: 'su-actions',
        children: [
          jsx('span', { className: 'su-muted', children: chosen.length ? `${count(chosen.length, 'item')} · ${bytes(chosen.reduce((sum, item) => sum + item.allocatedBytes, 0))}` : 'Nothing selected' }),
          jsx(Button, { size: 'sm', disabled: !chosen.length || working, onClick: review, children: working ? 'Checking…' : 'Next' })
        ]
      })
    ]
  })
}

function CommandCard({ store, command, onDone }) {
  const [params, setParams] = useState(() => {
    const initial = {}
    for (const [name, values] of Object.entries(command.params || {})) if (values.length) initial[name] = name === 'days' && values.includes(90) ? 90 : values[0]
    return initial
  })
  const [preview, setPreview] = useState(null)
  const [accepted, setAccepted] = useState(false)
  const [error, setError] = useState(null)
  const [working, setWorking] = useState(false)
  const [jobId, setJobId] = useState(null)
  const job = useQuery({
    queryKey: [ID, 'job', store.scopeKey, jobId],
    queryFn: () => rest(`/commands/jobs/${encodeURIComponent(jobId)}`),
    enabled: Boolean(jobId),
    refetchInterval: query => (!query.state.data || query.state.data.state === 'running' ? 1200 : false),
    retry: false
  })
  useEffect(() => { if (job.data && job.data.state !== 'running') onDone() }, [job.data?.state])
  const shown = Object.entries(params).reduce((text, [name, value]) => text.replace(`<${name}>`, String(value)), command.display)
  const running = job.data?.state === 'running'
  const finished = Boolean(job.data && !running)
  const ready = command.needsPreview ? Boolean(preview && preview.matchCount > 0) : true

  const runPreview = async () => {
    setWorking(true); setError(null); setAccepted(false)
    try { setPreview(await store.post(`/commands/${command.commandId}/preview`, { params }, { timeoutMs: 200000 })) } catch (failure) { setError(errorText(failure)) } finally { setWorking(false) }
  }
  const run = async () => {
    setWorking(true); setError(null)
    try {
      const started = await store.post(`/commands/${command.commandId}/run`, {
        params,
        previewToken: preview?.previewToken || null,
        confirmation: { commandId: command.commandId, acknowledged: true }
      })
      setJobId(started.jobId); setPreview(null); setAccepted(false)
    } catch (failure) { setError(errorText(failure)) } finally { setWorking(false) }
  }

  return jsxs('div', {
    className: 'su-cmd',
    children: [
      jsx('h3', { children: command.label }),
      jsx('p', { className: 'su-muted', children: command.description }),
      jsxs('p', { className: 'su-meta', children: ['Runs ', jsx('code', { children: shown }), ' on the serving host — Hermes’s own command, with its own safety rules.'] }),
      !command.available && jsx('div', { className: 'su-note su-warn', children: command.reason || 'Not available on this host.' }),
      ...Object.entries(command.params || {}).map(([name, values]) => jsxs('label', {
        className: 'su-muted',
        style: { display: 'flex', gap: 8, alignItems: 'center', margin: '6px 0', minWidth: 0 },
        children: [
          name === 'days' ? 'Older than' : name === 'repo' ? 'Repository' : name,
          jsx('select', {
            value: String(params[name]),
            style: { minWidth: 0, maxWidth: '100%' },
            onChange: event => { const raw = event.target.value; setParams(previous => ({ ...previous, [name]: name === 'days' ? Number(raw) : raw })); setPreview(null) },
            children: values.map(value => jsx('option', { value: String(value), title: String(value), children: name === 'days' ? `${value} days` : String(value).split('/').pop() }, String(value)))
          })
        ]
      }, name)),
      preview && jsxs('div', {
        className: `su-note ${preview.matchCount ? '' : 'su-ok'}`,
        children: [
          preview.matchCount === null || preview.matchCount === undefined
            ? 'Could not read the dry-run summary; see the list below.'
            : preview.matchCount ? `${count(preview.matchCount, preview.unit || 'item')} would be removed.` : 'Nothing to remove — no action needed.',
          preview.skippedOpen ? ` ${preview.skippedOpen} still-open sessions are skipped.` : '',
          preview.kept ? ` ${preview.kept} kept by Hermes's safety rules.` : '',
          preview.sample?.length ? jsx('pre', { children: preview.sample.join('\n') }) : null
        ]
      }),
      job.data && jsxs('div', {
        children: [
          jsx('div', {
            className: `su-note ${finished ? (job.data.state === 'succeeded' ? 'su-ok' : 'su-warn') : ''}`,
            children: !finished
              ? 'Running…'
              : job.data.state === 'succeeded'
                ? `Finished. ${job.data.ownedBytesRemoved != null ? `${bytes(job.data.ownedBytesRemoved)} removed from Hermes; ` : ''}free space on the disk changed by ${signed(job.data.volumeFreeDelta)}.`
                : `The command stopped with an error (exit ${job.data.exitCode ?? 'unknown'}). See its output below.`
          }),
          job.data.output ? jsxs('details', { children: [jsx('summary', { className: 'su-meta', children: 'Command output' }), jsx('pre', { children: job.data.output })] }) : null
        ]
      }),
      ready && command.available && !running && jsxs('label', { className: 'su-check', children: [jsx('input', { type: 'checkbox', checked: accepted, onChange: event => setAccepted(event.target.checked) }), jsx('span', { children: command.consequence })] }),
      error && jsx('div', { className: 'su-error', children: error }),
      jsxs('div', {
        className: 'su-actions su-end',
        children: [
          command.needsPreview ? jsx(Button, { size: 'sm', variant: 'outline', disabled: working || !command.available || running, onClick: runPreview, children: preview ? 'Preview again' : 'Preview (changes nothing)' }) : null,
          jsx(Button, { size: 'sm', variant: command.destructive ? 'destructive' : 'default', disabled: working || !command.available || !ready || !accepted || running, onClick: run, children: working ? 'Working…' : command.label })
        ]
      })
    ]
  })
}

function Guidance({ guidance }) {
  const [copied, setCopied] = useState(null)
  const copy = async text => {
    try { await navigator.clipboard.writeText(text); setCopied(text); setTimeout(() => setCopied(null), 1500) } catch (_) { setCopied(null) }
  }
  return jsxs('div', {
    children: [
      jsx('p', { className: 'su-muted', children: guidance.why }),
      guidance.warning && jsx('div', { className: 'su-note su-warn', children: guidance.warning }),
      guidance.holders?.length ? jsxs('div', { className: 'su-note', children: ['Open right now by: ', guidance.holders.map(row => `${row.unit || row.command} (PID ${row.pid})`).join(', ')] }) : null,
      guidance.crashLooping?.length ? jsx('div', { className: 'su-note su-warn', children: `${guidance.crashLooping.join(', ')} is restarting in a loop. That is separate from storage, but worth checking with systemctl status.` }) : null,
      ...guidance.steps.map((step, index) => jsxs('div', {
        className: 'su-step',
        children: [
          jsx('b', { children: index + 1 }),
          jsxs('div', { children: [step.text, step.command ? jsxs('div', { className: 'su-codeline', children: [jsx('code', { children: step.command }), jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => void copy(step.command), children: copied === step.command ? 'Copied' : 'Copy' })] }) : null] })
        ]
      }, index))
    ]
  })
}

function CategoryDetail({ store, categoryId, onBack, onExplore }) {
  const detail = useQuery({
    queryKey: [ID, 'category', store.scopeKey, categoryId],
    queryFn: () => rest(`/cleanup/${encodeURIComponent(categoryId)}`, { timeoutMs: 120000 }),
    retry: false,
    refetchOnWindowFocus: false
  })
  const category = detail.data?.category
  const afterChange = () => { void detail.refetch(); store.refresh() }
  const tiers = store.data?.home?.tiers || {}
  const friendlyRow = [...(tiers.green || []), ...(tiers.yellow || []), ...(tiers.pause || [])].find(row => row.id === categoryId)
  return jsxs('div', {
    children: [
      jsx('button', { type: 'button', className: 'su-back', onClick: onBack, children: '← Back' }),
      friendlyRow && jsxs('div', { style: { marginBottom: 8 }, children: [jsx('h3', { style: { fontSize: 15 }, children: friendlyRow.title }), jsx(Answers, { answers: friendlyRow.answers })] }),
      detail.isError && jsx('div', { className: 'su-error', children: errorText(detail.error) }),
      !category && !detail.isError && jsx('div', { className: 'su-progress' }),
      category && jsxs(Fragment, {
        children: [
          jsxs('div', { className: 'su-scanline', children: [jsxs('h3', { style: { display: 'flex', gap: 6, alignItems: 'center' }, children: [icon(category.icon), category.title] }), jsx('strong', { className: 'su-num', children: categoryValue(category) })] }),
          jsx('p', { className: 'su-muted', children: category.explanation }),
          category.details?.repos?.length ? jsx('div', { className: 'su-list', style: { margin: '6px 0' }, children: category.details.repos.map(repo => jsxs('div', { className: 'su-item', children: [icon('repo'), jsxs('span', { children: [jsx('span', { className: 'su-path', title: repo.repo, children: repo.repo.split('/').pop() }), jsx('span', { className: 'su-meta', children: `${count(repo.worktrees, 'worktree')} · ${repo.repo}` })] }), jsx('span', { className: 'su-num', children: bytes(repo.bytes) })] }, repo.repo)) }) : null,
        category.details?.sessions != null && jsx('p', { className: 'su-meta', children: `${count(category.details.sessions, 'session')} · ${count(category.details.messages, 'message')} · ${bytes(category.details.liveBytes)} in use · ${bytes(category.details.emptyBytes)} empty` }),
          category.reclaimable?.bytes ? jsx('div', { className: 'su-note su-ok', children: `${bytes(category.reclaimable.bytes)} reclaimable (${category.reclaimable.label || category.reclaimable.certainty}).` }) : null,
          category.action === 'review' && jsx(ReviewItems, { store, category, trash: detail.data.trash, onDone: afterChange }, category.id),
          category.action === 'command' && (category.commands.length
            ? category.commands.map(command => jsx(CommandCard, { store, command, onDone: afterChange }, command.commandId))
            : jsx('p', { className: 'su-muted', children: 'Nothing to do here right now.' })),
          category.action === 'projects' && jsx(ProjectsView, { store, category, onRescanned: afterChange }),
          category.action === 'guidance' && (category.guidance
            ? jsx(Guidance, { guidance: category.guidance })
            : jsx('div', { className: 'su-note su-ok', children: 'The session database has little empty space. Nothing to do.' })),
          onExplore && category.action === 'review' && category.scope !== 'projects' && category.items?.length ? jsx('button', { type: 'button', className: 'su-link', style: { marginTop: 10 }, onClick: () => onExplore(category), children: 'See where this is in Explorer →' }) : null
        ]
      })
    ]
  })
}

function ProjectsView({ store, category, onRescanned }) {
  const [error, setError] = useState(null)
  const details = category.details || {}
  const scanning = Boolean(details.scan?.running)
  useEffect(() => {
    if (!scanning) return undefined
    const timer = setInterval(onRescanned, 3000)
    return () => clearInterval(timer)
  }, [scanning])
  const rescan = async () => {
    setError(null)
    try { await store.post('/projects/scan'); onRescanned() } catch (failure) { setError(errorText(failure)) }
  }
  const projects = details.projects || []
  return jsxs('div', {
    children: [
      jsxs('div', { className: 'su-scanline', children: [
        jsx('span', { children: scanning ? 'Scanning folders Hermes created…' : details.scannedAt ? `Scanned ${ago(details.scannedAt)}${details.state === 'partial' ? ` · stopped early (${details.budgetReason})` : ''}` : 'Not scanned yet' }),
        jsx(Button, { size: 'sm', variant: 'ghost', disabled: scanning, onClick: rescan, children: scanning ? 'Scanning…' : 'Rescan folders' })
      ] }),
      scanning && jsx('div', { className: 'su-progress' }),
      details.scan?.error && jsx('div', { className: 'su-error', children: details.scan.error }),
      error && jsx('div', { className: 'su-error', children: error }),
      projects.length > 0 && jsx('div', { className: 'su-list', children: projects.map(project => jsxs('div', {
        className: 'su-item',
        children: [
          icon(project.isRepo ? 'repo' : 'folder'),
          jsxs('span', { children: [
            jsx('span', { className: 'su-path', title: project.root, children: project.root.replace(/^\/root\//, '~/') }),
            jsx('span', { className: 'su-meta', style: { whiteSpace: 'normal' }, children: [
              project.writes ? `Hermes wrote files here ${count(project.writes, 'time')}` : null,
              project.sessions ? `in ${count(project.sessions, 'session')}` : null,
              project.kanbanTasks ? `${count(project.kanbanTasks, 'Kanban task')}` : null,
              project.lastActivity ? `last ${ago(new Date(project.lastActivity * 1000).toISOString())}` : null,
              project.active ? 'in active use' : null
            ].filter(Boolean).join(' · ') })
          ] }),
          jsx('span', { className: 'su-num', children: bytes(project.allocatedBytes) })
        ]
      }, project.root)) })
    ]
  })
}

// ----- explorer --------------------------------------------------------------------

function Explorer({ store, path, setPath, onCategory, compact, onOpenPage }) {
  const view = useQuery({
    queryKey: [ID, 'explorer', store.scopeKey, store.data?.snapshot?.snapshotId, path],
    queryFn: () => rest(`/explorer?path=${encodeURIComponent(path)}`),
    enabled: Boolean(store.data?.snapshot),
    retry: false,
    refetchOnWindowFocus: false
  })
  if (!store.data) return jsx('div', { className: 'su-progress' })
  if (!store.data.snapshot) return jsx('p', { className: 'su-muted', children: 'Explorer is ready after the first scan finishes.' })
  if (view.isError) return jsxs('div', { children: [jsx('div', { className: 'su-error', children: errorText(view.error) }), jsx('button', { type: 'button', className: 'su-link', onClick: () => setPath(''), children: 'Back to the Hermes home' })] })
  const data = view.data
  if (!data) return jsx('div', { className: 'su-progress' })
  const byPath = new Map([...data.children, ...data.files].map(row => [row.path, row]))
  const tiles = [
    ...data.children.map(child => ({ kind: 'dir', path: child.path, name: child.name, label: child.name, value: child.allocatedBytes, description: child.description })),
    ...data.files.map(file => ({ kind: 'file', path: file.path, name: file.name, label: file.name, value: file.allocatedBytes, description: file.description })),
    ...(data.otherBytes > 0 ? [{ kind: 'other', path: '__other', name: 'other', label: data.children.length || data.files.length ? 'Smaller items (not itemized)' : 'Contents (each item too small to itemize)', value: data.otherBytes }] : [])
  ].sort((a, b) => b.value - a.value)
  const rows = compact ? tiles.slice(0, 12) : tiles
  return jsxs('div', {
    children: [
      jsx('div', {
        className: 'su-crumbs',
        children: data.breadcrumbs.map((crumb, index) => jsxs(Fragment, { children: [
          index > 0 ? jsx('span', { className: 'su-meta', children: '/' }) : null,
          jsx('button', { type: 'button', onClick: () => setPath(crumb.path), children: crumb.path === '' ? 'Hermes home' : crumb.name })
        ] }, crumb.path || 'home'))
      }),
      jsx('p', { className: 'su-meta', children: [bytes(data.node.allocatedBytes), count(data.node.files, 'file'), data.node.description].filter(Boolean).join(' · ') }),
      jsx(Treemap, { items: tiles, onOpen: setPath }),
      jsx('div', {
        className: 'su-list',
        children: rows.map(row => {
          const source = byPath.get(row.path)
          return jsxs('div', {
            className: `su-item ${row.kind === 'dir' ? 'su-click' : ''}`,
            onClick: row.kind === 'dir' ? () => setPath(row.path) : undefined,
            children: [
              icon(row.kind === 'dir' ? 'folder' : row.kind === 'file' ? 'file' : 'ellipsis'),
              jsxs('span', { children: [
                jsxs('span', { className: 'su-path', title: row.path, children: [
                  row.label,
                  source?.categoryId ? jsx('button', { type: 'button', className: 'su-badge', onClick: event => { event.stopPropagation(); onCategory(source.categoryId) }, children: 'clean up' }) : null
                ] }),
                row.description ? jsx('span', { className: 'su-desc', children: row.description }) : null
              ] }),
              jsx('span', { className: 'su-num', children: bytes(row.value) })
            ]
          }, row.path)
        })
      }),
      path === '' && data.privacy?.sensitiveFiles > 0 && !data.privacy?.namesShown && jsx('p', { className: 'su-meta', children: `${count(data.privacy.sensitiveFiles, 'credential-looking file')} in this Hermes home are counted in sizes but not named. You can change this under Privacy.` }),
      compact && onOpenPage && jsxs('div', { className: 'su-actions su-end', children: [jsx('button', { type: 'button', className: 'su-link', onClick: onOpenPage, children: 'Open full Explorer →' })] })
    ]
  })
}

// ----- trash -----------------------------------------------------------------------

function TrashView({ store }) {
  const trash = useQuery({ queryKey: [ID, 'trash', store.scopeKey], queryFn: () => rest('/trash'), retry: false, refetchOnWindowFocus: false })
  const [selected, setSelected] = useState(() => new Set())
  const [accepted, setAccepted] = useState(false)
  const [message, setMessage] = useState(null)
  const [working, setWorking] = useState(false)
  const items = trash.data?.items || []
  const present = items.filter(item => item.status === 'present')
  const act = async kind => {
    setWorking(true); setMessage(null)
    try {
      const result = await store.post(kind === 'restore' ? '/trash/restore' : '/trash/remove', { receiptIds: [...selected], confirmation: { acknowledged: true } }, { timeoutMs: 300000 })
      const done = result.outcomes.filter(row => row.status === 'applied').length
      const problems = result.outcomes.filter(row => row.status !== 'applied' && row.status !== 'missing')
      setMessage({
        ok: !problems.length,
        text: `${kind === 'restore' ? 'Brought back' : 'Deleted for good'} ${count(done, 'item')}.${kind === 'remove' ? ` You got back ${bytes(Math.max(0, result.volumeFreeDelta || 0))}.` : ''}${problems.length ? ` ${problems.length} skipped: ${problems.map(row => row.reason).join('; ')}` : ''}`
      })
      setSelected(new Set()); setAccepted(false); void trash.refetch(); store.refresh()
    } catch (failure) { setMessage({ ok: false, text: errorText(failure) }) } finally { setWorking(false) }
  }
  if (trash.isError) return jsx('div', { className: 'su-error', children: errorText(trash.error) })
  if (!trash.data) return jsx('div', { className: 'su-progress' })
  return jsxs('div', {
    children: [
      jsx('p', { className: 'su-muted', children: `Things you removed in Tune-up wait here for 30 days so you can bring them back. After that they’re deleted and the space is given back automatically. Right now they use ${bytes(trash.data.presentBytes)}.` }),
      jsx('p', { className: 'su-meta', children: trash.data.note }),
      !items.length && jsx('p', { className: 'su-muted', children: 'The Trash is empty.' }),
      items.length > 0 && jsx('div', {
        className: 'su-list',
        children: items.map(item => jsxs('label', {
          className: `su-item ${item.status === 'present' ? '' : 'su-disabled'}`,
          children: [
            jsx('input', { type: 'checkbox', disabled: item.status !== 'present', checked: selected.has(item.receiptId), onChange: () => setSelected(previous => toggleIn(previous, item.receiptId)), 'aria-label': `Select ${item.displayPath || item.originalPath}` }),
            jsxs('span', { children: [
              jsx('span', { className: 'su-path', title: item.displayPath || item.originalPath, children: item.displayPath || item.originalPath }),
              jsx('span', { className: 'su-meta', children: `${item.status === 'present' ? `Removed ${ago(item.trashedAt)} · deleted for good in ${Math.max(0, 30 - Math.floor((Date.now() - Date.parse(item.trashedAt)) / 86400000))} days` : item.status === 'missing' ? `Already emptied outside Hermes · ${ago(item.trashedAt)}` : `Changed outside Hermes, left alone · ${ago(item.trashedAt)}`}` })
            ] }),
            jsx('span', { className: 'su-num', children: bytes(item.allocatedBytes) })
          ]
        }, item.receiptId))
      }),
      present.length > 0 && jsx('button', { type: 'button', className: 'su-link', style: { margin: '6px 0', display: 'inline-block' }, onClick: () => setSelected(selected.size === present.length ? new Set() : new Set(present.map(item => item.receiptId))), children: selected.size === present.length ? 'Clear selection' : 'Select all' }),
      selected.size > 0 && jsxs('label', { className: 'su-check', children: [jsx('input', { type: 'checkbox', checked: accepted, onChange: event => setAccepted(event.target.checked) }), jsx('span', { children: 'I understand “Delete now” can’t be undone. (“Bring back” puts things where they were and never overwrites anything.)' })] }),
      message && jsx('div', { className: `su-note ${message.ok ? 'su-ok' : 'su-warn'}`, children: message.text }),
      jsxs('div', {
        className: 'su-actions su-end',
        children: [
          jsx(Button, { size: 'sm', variant: 'outline', disabled: !selected.size || !accepted || working, onClick: () => void act('restore'), children: 'Bring back' }),
          jsx(Button, { size: 'sm', variant: 'destructive', disabled: !selected.size || !accepted || working, onClick: () => void act('remove'), children: 'Delete now' })
        ]
      })
    ]
  })
}

// ----- privacy & diagnostics -------------------------------------------------------

function PrivacyView({ store }) {
  const settings = store.data?.settings || {}
  const snapshot = store.data?.snapshot
  const [error, setError] = useState(null)
  const [duplicates, setDuplicates] = useState(null)
  const [advisor, setAdvisor] = useState(null)
  const [consent, setConsent] = useState(false)
  const [answer, setAnswer] = useState(null)
  const [working, setWorking] = useState(null)
  const advisorAvailable = store.capabilities.data?.capabilities?.advisor?.status === 'available'
  const toggleNames = async value => {
    setError(null)
    try { await store.post('/settings', { showSensitiveNames: value }); store.refresh() } catch (failure) { setError(errorText(failure)) }
  }
  const runDuplicates = async () => {
    setWorking('dup'); setError(null)
    try { setDuplicates(await store.post('/diagnostics/duplicates', {}, { timeoutMs: 120000 })) } catch (failure) { setError(errorText(failure)) } finally { setWorking(null) }
  }
  const previewAdvisor = async () => {
    setWorking('adv'); setError(null); setAnswer(null); setConsent(false)
    try { setAdvisor(await store.post('/advisor/preview', {}, { timeoutMs: 120000 })) } catch (failure) { setError(errorText(failure)) } finally { setWorking(null) }
  }
  const ask = async () => {
    setWorking('ask'); setError(null)
    try { setAnswer(await store.post('/advisor/complete', { consent: true, payloadDigest: advisor.payloadDigest }, { timeoutMs: 90000 })) } catch (failure) { setError(errorText(failure)) } finally { setWorking(null) }
  }
  const coverage = snapshot?.coverage
  const reasons = coverage?.excludedReasons || {}
  return jsxs('div', {
    children: [
      jsx('h4', { children: 'What Tune-up reads' }),
      jsx('p', { className: 'su-muted', children: 'Scans read only names, sizes and dates inside this Hermes home. Other profiles, other disks and the rest of your computer are not inventoried. File contents are never read during a scan.' }),
      jsxs('label', {
        className: 'su-check',
        children: [
          jsx('input', { type: 'checkbox', checked: Boolean(settings.showSensitiveNames), onChange: event => void toggleNames(event.target.checked) }),
          jsx('span', { children: 'Show the names of credential-looking files (.env, auth.json, keys, secrets…) in Explorer. Off by default. Their sizes always count. Either way they are never opened, hashed, sent to AI or offered for deletion. Applies from the next scan.' })
        ]
      }),
      coverage && jsx('p', {
        className: 'su-meta',
        children: [
          `Last scan: ${count(coverage.visited, 'entry', 'entries')} visited`,
          reasons.other_profiles ? `${reasons.other_profiles} other-profile folder skipped` : null,
          reasons.symlink ? `${reasons.symlink} links not followed` : null,
          reasons.mount ? `${reasons.mount} other volumes skipped` : null,
          coverage.unreadable ? `${coverage.unreadable} unreadable` : null,
          snapshot?.privacy?.sensitiveFiles ? `${snapshot.privacy.sensitiveFiles} credential-looking files ${snapshot.privacy.namesShown ? 'shown' : 'hidden'}` : null
        ].filter(Boolean).join(' · ')
      }),
      jsx('h4', { children: 'Duplicate files' }),
      jsx('p', { className: 'su-muted', children: 'Compares the contents of the largest files from the last scan, locally on the serving host. Databases and credential-looking files are never opened.' }),
      jsx(Button, { size: 'sm', variant: 'outline', disabled: !snapshot || working === 'dup', onClick: runDuplicates, children: working === 'dup' ? 'Comparing…' : 'Find duplicates' }),
      duplicates && jsxs('div', {
        children: [
          jsx('div', {
            className: `su-note ${duplicates.groups.length ? '' : 'su-ok'}`,
            children: duplicates.groups.length
              ? `${count(duplicates.groups.length, 'set')} of identical files · ${bytes(duplicates.extraCopiesBytes)} in extra copies${duplicates.complete ? '' : ' (stopped early at the read limit)'}`
              : `No duplicates among ${count(duplicates.filesHashed, 'large file')} compared.`
          }),
          ...duplicates.groups.slice(0, 20).map((group, index) => jsxs('div', { className: 'su-cmd', children: [jsx('div', { className: 'su-meta', children: `${bytes(group.sizeBytes)} each` }), ...group.paths.map(path => jsx('div', { className: 'su-path', style: { fontSize: 12 }, title: path, children: path }, path))] }, index))
        ]
      }),
      jsx('h4', { children: 'Ask AI to explain (optional)' }),
      jsx('p', { className: 'su-muted', children: 'You see exactly what would be sent first: top-level folder totals and cleanup categories. No names inside your folders and no file contents. Nothing is sent until you agree.' }),
      jsxs('div', {
        className: 'su-actions su-start',
        children: [
          jsx(Button, { size: 'sm', variant: 'outline', disabled: !snapshot || working === 'adv', onClick: previewAdvisor, children: 'Show what would be sent' }),
          jsx('button', { type: 'button', className: 'su-link', onClick: () => host.navigate('/settings?tab=config:model&aux=storage_advisor'), children: 'Choose model' })
        ]
      }),
      !advisorAvailable && jsx('p', { className: 'su-meta', children: 'The Tune-up helper is not registered in this Hermes process yet. Restart Hermes after enabling the plugin to use it.' }),
      advisor && jsxs('div', {
        children: [
          jsx('pre', { children: JSON.stringify(advisor.payload, null, 2) }),
          jsxs('label', { className: 'su-check', children: [jsx('input', { type: 'checkbox', checked: consent, onChange: event => setConsent(event.target.checked) }), jsx('span', { children: 'Send exactly this summary to my Tune-up helper model.' })] }),
          jsx(Button, { size: 'sm', disabled: !consent || !advisor.available || working === 'ask', onClick: ask, children: working === 'ask' ? 'Asking…' : 'Explain my storage' })
        ]
      }),
      answer && jsxs('div', { className: 'su-note', children: [jsx('div', { style: { whiteSpace: 'pre-wrap' }, children: answer.text }), jsx('div', { className: 'su-meta', children: `${answer.provider || 'provider'} · ${answer.model || 'model'} · advice only. Every action is still reviewed here.` })] }),
      error && jsx('div', { className: 'su-error', children: error })
    ]
  })
}

// ----- shells ----------------------------------------------------------------------

function Header({ store, onOpenFull, onClose }) {
  const profile = useValue(host.state.profile)
  const backend = store.capabilities.data?.backend
  const machine = backend?.host || 'the Hermes computer'
  const serverVersion = backend?.version || null
  const mismatch = Boolean(store.capabilities.data) && olderThan(serverVersion, SERVER_NEEDS)
  return jsxs('div', {
    children: [
      jsxs('div', { className: 'su-titlerow', children: [
        jsx('h2', { children: backend ? `Tune-up · ${machine}` : 'Tune-up' }),
        onOpenFull && jsx('button', { type: 'button', className: 'su-full', onClick: onOpenFull, title: 'Open Tune-up in a pane beside your chat', children: 'Open full view ⧉' }),
        onClose && jsx('button', { type: 'button', className: 'su-full', onClick: onClose, 'aria-label': 'Close Tune-up', title: 'Close Tune-up and go back to just your chat', children: 'Close ✕' })
      ] }),
      jsx('div', { className: 'su-meta', children: [`The computer Hermes runs on${backend?.os ? ` (${backend.os})` : ''}, not the device you're viewing from`, profile ? `profile ${profile}` : null].filter(Boolean).join(' · ') }),
      mismatch && jsx('div', { className: 'su-note su-warn', children: `Tune-up on ${machine} is ${serverVersion ? `version ${serverVersion}` : 'an older version'}, but this screen is ${VERSION}. To finish updating, restart the Hermes dashboard service on ${machine} (sudo systemctl restart hermes-dashboard). Until then some information may be missing.` })
    ]
  })
}

function Tabs({ tabs, value, onChange }) {
  return jsx('div', {
    className: 'su-tabs',
    role: 'tablist',
    children: tabs.map(([id, label]) => jsx('button', { type: 'button', role: 'tab', 'aria-selected': value === id ? 'true' : 'false', onClick: () => onChange(id), children: label }, id))
  })
}

function parentOf(category) {
  return category.items?.[0]?.path?.split('/').slice(0, -1).join('/') || ''
}

function StoragePanel() {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState('overview')
  const [categoryId, setCategoryId] = useState(null)
  const [path, setPath] = useState('')
  const store = useStorage(open)
  const status = useQuery({
    queryKey: [ID, 'status', store.scopeKey],
    queryFn: () => rest('/status'),
    enabled: Boolean(store.binding),
    refetchInterval: query => (query.state.data?.scan && !query.state.data.scan.finished ? 5000 : 300000),
    retry: false,
    refetchOnWindowFocus: false
  })
  // Bounded periodic refresh while Hermes is open: at most one automatic scan a day.
  useEffect(() => {
    const data = status.data
    if (!data || !store.binding || (data.scan && !data.scan.finished)) return
    const finished = data.snapshot?.finishedAt ? Date.parse(data.snapshot.finishedAt) : 0
    if (Date.now() - finished > AUTO_SCAN_AGE_MS && Date.now() - lastAutoStart > 30 * 60000) void store.startScan().catch(() => {})
  }, [status.data?.snapshot?.finishedAt, status.data?.scan?.finished, store.binding])
  useEffect(() => { setCategoryId(null); setPath('') }, [store.scopeKey])

  const volume = status.data?.capacity
  const low = volume?.availableBytes != null && volume.totalBytes && volume.availableBytes / volume.totalBytes < 0.1
  const hermes = status.data?.snapshot?.allocatedBytes
  const openPage = () => { setOpen(false); openFull() }
  const label = volume ? `Tune-up: Hermes uses ${bytes(hermes)}, ${bytes(volume.availableBytes)} free on disk` : 'Tune-up'

  return jsxs(Popover, {
    open,
    // Always open on the overview (the approved panel), not on the last tab used.
    onOpenChange: value => { if (value) { setTab('overview'); setCategoryId(null) } setOpen(value) },
    children: [
      jsx(PopoverTrigger, { asChild: true, children: jsxs('button', { type: 'button', className: `su-trigger ${low ? 'su-low' : ''}`, 'aria-label': label, title: label, children: [icon('database'), jsx('strong', { children: volume ? `${bytes(volume.availableBytes)} free` : 'Tune-up' })] }) }),
      jsx(PopoverContent, {
        align: 'end',
        side: 'top',
        className: 'su su-panel',
        'aria-label': 'Tune-up',
        children: open && jsxs(Fragment, {
          children: [
            jsx(Header, { store, onOpenFull: openPage }),
            jsx(Tabs, { tabs: [['overview', 'Clean up'], ['checkup', 'Checkup'], ['explorer', 'Explore'], ['trash', 'Trash'], ['privacy', 'Settings']], value: tab, onChange: value => { setTab(value); setCategoryId(null) } }),
            jsxs('div', { className: 'su-panel-body', children: [
            store.capabilities.isError && jsx('div', { className: 'su-error', children: `Tune-up could not reach this Hermes instance: ${errorText(store.capabilities.error)}` }),
            tab === 'overview' && (categoryId
              ? jsx(CategoryDetail, { store, categoryId, onBack: () => setCategoryId(null), onExplore: category => { setPath(parentOf(category)); setTab('explorer'); setCategoryId(null) } })
              : jsx(Home, { store, onCategory: setCategoryId, onExplorer: openPage, onCheckup: () => setTab('checkup') })),
            tab === 'checkup' && jsx(CheckupView, { store }),
            tab === 'explorer' && jsx(Explorer, { store, path, setPath, compact: true, onOpenPage: openPage, onCategory: id => { setTab('overview'); setCategoryId(id) } }),
            tab === 'trash' && jsx(TrashView, { store }),
            tab === 'privacy' && jsx(PrivacyView, { store })
            ] })
          ]
        })
      })
    ]
  })
}

function StoragePage({ onBack, onClose }) {
  const store = useStorage(true)
  const [tab, setTab] = useState('explorer')
  const [categoryId, setCategoryId] = useState(null)
  const [path, setPath] = useState('')
  useEffect(() => { setCategoryId(null); setPath('') }, [store.scopeKey])
  const showCategory = id => { setCategoryId(id); setTab('cleanup') }
  return jsx('div', {
    className: 'su su-page',
    children: jsxs('div', {
      className: 'su-page-inner',
      children: [
        jsxs('section', { children: [onBack ? jsx('button', { type: 'button', className: 'su-back', onClick: onBack, children: '← Back to chat' }) : null, jsx(Header, { store, onClose }), jsx('div', { style: { height: 12 } }), store.capabilities.isError ? jsx('div', { className: 'su-error', children: errorText(store.capabilities.error) }) : null, jsx(Home, { store, onCategory: showCategory, onCheckup: () => setTab('checkup') })] }),
        jsxs('section', {
          children: [
            jsx(Tabs, { tabs: [['checkup', 'Checkup'], ['explorer', 'Explore'], ...(categoryId ? [['cleanup', 'Details']] : []), ['trash', 'Trash'], ['privacy', 'Settings']], value: tab, onChange: setTab }),
            tab === 'checkup' && jsx(CheckupView, { store }),
            tab === 'explorer' && jsx(Explorer, { store, path, setPath, onCategory: showCategory }),
            tab === 'cleanup' && categoryId && jsx(CategoryDetail, { store, categoryId, onBack: () => { setCategoryId(null); setTab('explorer') }, onExplore: category => { setPath(parentOf(category)); setTab('explorer') } }, categoryId),
            tab === 'trash' && jsx(TrashView, { store }),
            tab === 'privacy' && jsx(PrivacyView, { store })
          ]
        })
      ]
    })
  })
}

// The sidebar row can only navigate to a route. Landing here opens the docked pane and
// steps straight back to whatever the user was doing, so the chat is never replaced.
function StorageRoute() {
  const docked = useRef(false)
  useEffect(() => {
    if (!canDock() || docked.current) return
    docked.current = true
    openFull()
    if (window.history.length > 1) window.history.back()
    else host.navigate('/')
  }, [])
  if (canDock()) return jsx('div', { className: 'su su-page', children: jsx('p', { className: 'su-muted', style: { padding: 20 }, children: 'Opening Tune-up beside your chat…' }) })
  return jsx(StoragePage, { onBack: () => { if (window.history.length > 1) window.history.back(); else host.navigate('/') } })
}

export default {
  id: ID,
  name: 'Tune-up',
  description: 'Keep Hermes fast, healthy and tidy: free up space safely, check its health, and stop what agents left running.',
  defaultEnabled: true,
  register(ctx) {
    CTX = ctx
    const style = document.createElement('style')
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => { style.remove(); if (CTX === ctx) CTX = null })
    ctx.register({ id: 'storage', area: STATUSBAR_AREAS.right, order: 20, render: () => jsx(StoragePanel, {}) })
    if (ROUTES_AREA) ctx.register({ id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(StorageRoute, {}) })
    ctx.onDispose(() => { if (closeFull) { const close = closeFull; closeFull = null; try { close() } catch (_) {} } })
    // Sidebar row next to Kanban (50) and Resetwatch (62): opens the full Tune-up page.
    if (ROUTES_AREA && SIDEBAR_NAV_AREA) ctx.register({ id: 'nav', area: SIDEBAR_NAV_AREA, order: 64, data: { path: ROUTE, label: 'Tune-up', codicon: 'pulse' } })
    if (PALETTE_AREA) ctx.register({ id: 'open', area: PALETTE_AREA, data: { id: 'storage-utility.open', label: 'Tune-up: Open beside chat', keywords: ['disk', 'space', 'cleanup', 'storage', 'junk', 'health', 'tune'], run: () => { openFull() } } })
  }
}
