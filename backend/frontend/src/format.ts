const timeFormatter = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})

export function formatTime(value?: string | null): string {
  return value ? timeFormatter.format(new Date(value)) : '--'
}

export function formatDuration(ms?: number | null): string {
  if (!ms) return '--'
  if (ms < 1_000) return `${ms} ms`
  if (ms < 60_000) return `${Math.round(ms / 1_000)} 秒`
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1_000)
  return `${minutes} 分 ${seconds} 秒`
}

export function formatNumber(value?: number | null): string {
  return value == null ? '--' : new Intl.NumberFormat('zh-CN').format(value)
}

export function shortId(value?: string | null): string {
  return value ? value.slice(0, 8) : '--'
}

export function relativeTime(value?: string | null): string {
  if (!value) return '--'
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1_000))
  if (seconds < 60) return `${seconds} 秒前`
  if (seconds < 3_600) return `${Math.floor(seconds / 60)} 分钟前`
  return `${Math.floor(seconds / 3_600)} 小时前`
}
