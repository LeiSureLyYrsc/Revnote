import StarterKit from '@tiptap/starter-kit'
import { Markdown } from '@tiptap/markdown'

export const editorExtensions = [
  StarterKit.configure({
    heading: {
      levels: [1, 2, 3],
    },
  }),
  Markdown,
]
