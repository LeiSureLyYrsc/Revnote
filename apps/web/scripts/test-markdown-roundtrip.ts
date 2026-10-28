import { Editor } from '@tiptap/core'
import { editorExtensions } from '../src/components/editor/extensions'

interface TestCase {
  name: string
  markdown: string
  assertInitialAst: (ast: any) => void
}

const testCases: TestCase[] = [
  {
    name: 'Headings (H1, H2, H3)',
    markdown: '# 标题一\n\n## 标题二\n\n### 标题三',
    assertInitialAst: (ast) => {
      const headings = ast.content.filter((n: any) => n.type === 'heading')
      if (headings.length !== 3) {
        throw new Error(`期望 3 个 heading 节点，实际得到 ${headings.length}`)
      }
      const levels = headings.map((h: any) => h.attrs?.level)
      if (levels[0] !== 1 || levels[1] !== 2 || levels[2] !== 3) {
        throw new Error(`Heading 等级不匹配: 期望 [1, 2, 3]，实际 ${JSON.stringify(levels)}`)
      }
    },
  },
  {
    name: 'Inline marks (Bold, Italic, Code, Strike)',
    markdown: '正文包含 **粗体文字**、*斜体文字*、~~删除线~~ 以及 `行内代码`。',
    assertInitialAst: (ast) => {
      const textNodes: any[] = []
      function collect(node: any) {
        if (node.type === 'text') textNodes.push(node)
        if (node.content) node.content.forEach(collect)
      }
      collect(ast)

      const marksFound = new Set<string>()
      for (const t of textNodes) {
        if (t.marks) {
          for (const m of t.marks) {
            marksFound.add(m.type)
          }
        }
      }
      for (const requiredMark of ['bold', 'italic', 'strike', 'code']) {
        if (!marksFound.has(requiredMark)) {
          throw new Error(`未解析到预期的行内 Mark: ${requiredMark}`)
        }
      }
    },
  },
  {
    name: 'Unordered List',
    markdown: '- 任务清单 A\n- 任务清单 B\n- 任务清单 C',
    assertInitialAst: (ast) => {
      const list = ast.content.find((n: any) => n.type === 'bulletList')
      if (!list) throw new Error('未找到 bulletList 节点')
      if (list.content?.length !== 3) {
        throw new Error(`期望 3 个列表项，实际得到 ${list.content?.length}`)
      }
    },
  },
  {
    name: 'Ordered List',
    markdown: '1. 第一步骤\n2. 第二步骤\n3. 第三步骤',
    assertInitialAst: (ast) => {
      const list = ast.content.find((n: any) => n.type === 'orderedList')
      if (!list) throw new Error('未找到 orderedList 节点')
      if (list.content?.length !== 3) {
        throw new Error(`期望 3 个有序列表项，实际得到 ${list.content?.length}`)
      }
    },
  },
  {
    name: 'Blockquote',
    markdown: '> Revnote 笔记同步内核设计规范\n> 保持数据幂等与版本一致性。',
    assertInitialAst: (ast) => {
      const quote = ast.content.find((n: any) => n.type === 'blockquote')
      if (!quote) throw new Error('未找到 blockquote 节点')
      const text = JSON.stringify(quote)
      if (!text.includes('Revnote 笔记同步内核设计规范')) {
        throw new Error('Blockquote 文本丢失')
      }
    },
  },
  {
    name: 'Code Block with language',
    markdown: '```typescript\ninterface NoteRevision {\n  rev: number;\n  title: string;\n}\n```',
    assertInitialAst: (ast) => {
      const codeBlock = ast.content.find((n: any) => n.type === 'codeBlock')
      if (!codeBlock) throw new Error('未找到 codeBlock 节点')
      if (codeBlock.attrs?.language !== 'typescript') {
        throw new Error(`代码块语言不匹配: 期望 typescript，实际 ${codeBlock.attrs?.language}`)
      }
      if (!codeBlock.content?.[0]?.text?.includes('interface NoteRevision')) {
        throw new Error('代码块内容缺失')
      }
    },
  },
  {
    name: 'Mixed Rich Text Document',
    markdown: [
      '# Revnote 架构纪要',
      '',
      'Revnote 采用 **Sync Kernel** 优先的设计思想。',
      '',
      '## 核心要点',
      '',
      '- 工作区序列分配器（严格递增）',
      '- CAS 乐观并发控制',
      '- 不可变对象存储关联',
      '',
      '> 核心约束：Web 端暂不实现假保存与模拟同步。',
      '',
      '```sql\nSELECT change_seq FROM workspaces WHERE id = :ws FOR UPDATE;\n```',
    ].join('\n'),
    assertInitialAst: (ast) => {
      const types = ast.content.map((n: any) => n.type)
      const expectedTypes = ['heading', 'paragraph', 'heading', 'bulletList', 'blockquote', 'codeBlock']
      for (const t of expectedTypes) {
        if (!types.includes(t)) {
          throw new Error(`复合文档初次解析缺失节点类型: ${t}`)
        }
      }
    },
  },
]

function normalizeAst(node: any): any {
  if (!node || typeof node !== 'object') return node
  const clean: any = {}
  for (const [key, val] of Object.entries(node)) {
    // Ignore transient/undefined values
    if (val === undefined) continue
    if (Array.isArray(val)) {
      clean[key] = val.map(normalizeAst)
    } else if (typeof val === 'object' && val !== null) {
      clean[key] = normalizeAst(val)
    } else {
      clean[key] = val
    }
  }
  return clean
}

function runMarkdownRoundtripVerification(): void {
  console.log('--- 启动 TipTap Markdown 语义往返保真度验证 ---')

  const editor = new Editor({
    extensions: editorExtensions,
    content: 'bootstrap',
    contentType: 'markdown',
  })

  let passed = 0
  let failed = 0

  for (const tc of testCases) {
    try {
      // 1. Initial Markdown -> AST1
      editor.commands.setContent(tc.markdown, { contentType: 'markdown' })
      const ast1 = normalizeAst(editor.getJSON())

      // Assert expected nodes, marks, and attributes on first parse
      tc.assertInitialAst(ast1)

      const serialized1 = editor.getMarkdown()

      // 2. Serialized Markdown -> AST2
      editor.commands.setContent(serialized1, { contentType: 'markdown' })
      const ast2 = normalizeAst(editor.getJSON())
      const serialized2 = editor.getMarkdown()

      const ast1Str = JSON.stringify(ast1)
      const ast2Str = JSON.stringify(ast2)
      const isAstEqual = ast1Str === ast2Str
      const isIdempotent = serialized1 === serialized2

      if (isAstEqual && isIdempotent) {
        console.log(`✓ [通过] ${tc.name}`)
        passed++
      } else {
        console.error(`✗ [失败] ${tc.name}`)
        if (!isAstEqual) {
          console.error('  AST 语义差异:')
          console.error('  AST 1:', JSON.stringify(ast1, null, 2))
          console.error('  AST 2:', JSON.stringify(ast2, null, 2))
        }
        if (!isIdempotent) {
          console.error('  Markdown 二次序列化不幂等:')
          console.error('  第一次输出:', JSON.stringify(serialized1))
          console.error('  第二次输出:', JSON.stringify(serialized2))
        }
        failed++
      }
    } catch (err: unknown) {
      console.error(`✗ [初次解析断言失败] ${tc.name}:`, err instanceof Error ? err.message : err)
      failed++
    }
  }

  editor.destroy()

  console.log('--------------------------------------------------')
  console.log(`验证完成: 共 ${testCases.length} 项，通过 ${passed} 项，失败 ${failed} 项`)

  if (failed > 0) {
    process.exit(1)
  }
}

runMarkdownRoundtripVerification()
