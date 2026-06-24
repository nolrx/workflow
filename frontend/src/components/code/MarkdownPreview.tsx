import ReactMarkdown from "react-markdown"
import { cn } from "@/lib/utils"

interface MarkdownPreviewProps {
  children: string
  className?: string
}

/**
 * Render AI-generated Markdown documents with theme-aware, typography-first
 * styling. Safe by default: react-markdown does not emit raw HTML.
 */
export function MarkdownPreview({ children, className }: MarkdownPreviewProps) {
  return (
    <div
      className={cn(
        "markdown-preview max-h-[60vh] overflow-y-auto pr-2 text-sm leading-relaxed text-foreground",
        className
      )}
    >
      <ReactMarkdown
        components={{
          h1: ({ children, ...props }) => (
            <h1
              {...props}
              className="mb-4 mt-2 border-b pb-2 text-2xl font-semibold text-foreground"
            >
              {children}
            </h1>
          ),
          h2: ({ children, ...props }) => (
            <h2
              {...props}
              className="mb-3 mt-5 text-xl font-semibold text-foreground"
            >
              {children}
            </h2>
          ),
          h3: ({ children, ...props }) => (
            <h3
              {...props}
              className="mb-2 mt-4 text-lg font-semibold text-foreground"
            >
              {children}
            </h3>
          ),
          h4: ({ children, ...props }) => (
            <h4
              {...props}
              className="mb-2 mt-3 text-base font-semibold text-foreground"
            >
              {children}
            </h4>
          ),
          p: ({ children, ...props }) => (
            <p {...props} className="mb-3 text-foreground">
              {children}
            </p>
          ),
          ul: ({ children, ...props }) => (
            <ul {...props} className="mb-3 list-disc space-y-1 pl-5 text-foreground">
              {children}
            </ul>
          ),
          ol: ({ children, ...props }) => (
            <ol {...props} className="mb-3 list-decimal space-y-1 pl-5 text-foreground">
              {children}
            </ol>
          ),
          li: ({ children, ...props }) => (
            <li {...props} className="text-foreground">
              {children}
            </li>
          ),
          code: ({ className, children, ...props }) => {
            // Inline <code> has no className; fenced code blocks come with
            // "language-xxx" className from react-markdown.
            const isInline = !className
            if (isInline) {
              return (
                <code
                  {...props}
                  className="rounded-sm bg-muted px-1 py-0.5 font-mono text-xs text-foreground"
                >
                  {children}
                </code>
              )
            }
            return (
              <pre className="mb-3 overflow-x-auto rounded-lg bg-muted p-3 font-mono text-xs text-foreground">
                <code {...props} className={className}>{children}</code>
              </pre>
            )
          },
          blockquote: ({ children, ...props }) => (
            <blockquote
              {...props}
              className="mb-3 border-l-2 border-primary pl-3 italic text-muted-foreground"
            >
              {children}
            </blockquote>
          ),
          a: ({ children, href, ...props }) => (
            <a
              {...props}
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary hover:underline"
            >
              {children}
            </a>
          ),
          hr: (props) => <hr {...props} className="my-4 border-border" />,
          table: ({ children, ...props }) => (
            <div className="mb-3 overflow-x-auto">
              <table {...props} className="min-w-full border-collapse text-sm">
                {children}
              </table>
            </div>
          ),
          thead: ({ children, ...props }) => (
            <thead {...props} className="bg-muted">
              {children}
            </thead>
          ),
          th: ({ children, ...props }) => (
            <th
              {...props}
              className="border border-border px-3 py-2 text-left font-semibold"
            >
              {children}
            </th>
          ),
          td: ({ children, ...props }) => (
            <td {...props} className="border border-border px-3 py-2">
              {children}
            </td>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
