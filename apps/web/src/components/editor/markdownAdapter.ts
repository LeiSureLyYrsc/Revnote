/** Markdown remains the source of truth. This adapter never parses on load. */
export interface MarkdownDocument { title: string; bodyMd: string }

export function withTitle(document: MarkdownDocument, title: string): MarkdownDocument {
  return { title, bodyMd: document.bodyMd }
}

/** Browsers normalize textarea line endings; retain the loaded convention on edits. */
export function applySourceInput(original: string, input: string): string {
  if (input === original.replace(/\r\n/g, '\n')) return original
  return original.includes('\r\n') && !original.replace(/\r\n/g, '').includes('\n')
    ? input.replace(/\r\n/g, '\n').replace(/\n/g, '\r\n')
    : input
}

export function downloadMarkdown(title: string, bodyMd: string): void {
  const url = URL.createObjectURL(new Blob([bodyMd], { type: 'text/markdown;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  const filename = [...title].map(character => character.charCodeAt(0) < 32 || '<>:"/\\|?*'.includes(character) ? '_' : character).join('')
  link.download = `${filename || 'note'}.md`
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
