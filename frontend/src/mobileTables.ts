import type { Directive } from 'vue'

function labelTables(root: HTMLElement) {
  for (const table of Array.from(root.querySelectorAll<HTMLTableElement>('table'))) {
    const headings = Array.from(table.querySelectorAll('thead th'), cell => cell.textContent?.trim() || '')
    for (const row of Array.from(table.querySelectorAll<HTMLTableRowElement>('tbody tr'))) {
      Array.from(row.cells).forEach((cell, index) => {
        cell.dataset.label = headings[index] || ''
      })
    }
  }
}

export const mobileTables: Directive<HTMLElement> = {
  mounted: labelTables,
  updated: labelTables,
}
