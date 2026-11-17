import React, { useState } from 'react';

interface SafeMarkdownProps {
  content: string;
  className?: string;
  style?: React.CSSProperties;
}

/**
 * Lightweight, zero-dependency, XSS-safe Markdown renderer for conversational messages
 * Supports headings, code blocks, lists, bold/italic, inline code, and sanitized links.
 */
export const SafeMarkdown: React.FC<SafeMarkdownProps> = ({ content, className = '', style }) => {
  if (!content) return null;

  // Split into lines for block-level parsing
  const lines = content.split('\n');
  const elements: React.ReactNode[] = [];

  let inCodeBlock = false;
  let codeBlockLang = '';
  let codeBlockLines: string[] = [];
  let currentList: string[] = [];
  let isNumberedList = false;

  const flushList = () => {
    if (currentList.length === 0) return;
    const ListTag = isNumberedList ? 'ol' : 'ul';
    const listIndex = elements.length;
    elements.push(
      <ListTag
        key={`list-${listIndex}`}
        style={{
          margin: '8px 0 8px 20px',
          paddingLeft: '6px',
          display: 'flex',
          flexDirection: 'column',
          gap: '4px',
        }}
      >
        {currentList.map((item, i) => (
          <li key={i} dir="auto" style={{ lineHeight: 1.5 }}>
            {renderInlineMarkdown(item)}
          </li>
        ))}
      </ListTag>
    );
    currentList = [];
    isNumberedList = false;
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Code block boundary
    if (line.trim().startsWith('```')) {
      flushList();
      if (inCodeBlock) {
        // End code block
        const codeText = codeBlockLines.join('\n');
        const blockIndex = elements.length;
        elements.push(
          <CodeBlockCard
            key={`code-${blockIndex}`}
            code={codeText}
            language={codeBlockLang || 'text'}
          />
        );
        inCodeBlock = false;
        codeBlockLines = [];
        codeBlockLang = '';
      } else {
        // Start code block
        inCodeBlock = true;
        codeBlockLang = line.trim().slice(3).trim();
      }
      continue;
    }

    if (inCodeBlock) {
      codeBlockLines.push(line);
      continue;
    }

    // Headings
    if (line.startsWith('### ')) {
      flushList();
      elements.push(
        <h4
          key={`h4-${i}`}
          dir="auto"
          style={{
            fontSize: '0.9375rem',
            fontWeight: 700,
            margin: '10px 0 4px',
            color: 'var(--text-primary)',
          }}
        >
          {renderInlineMarkdown(line.slice(4))}
        </h4>
      );
      continue;
    }
    if (line.startsWith('## ')) {
      flushList();
      elements.push(
        <h3
          key={`h3-${i}`}
          dir="auto"
          style={{
            fontSize: '1.0625rem',
            fontWeight: 700,
            margin: '12px 0 6px',
            color: 'var(--text-primary)',
          }}
        >
          {renderInlineMarkdown(line.slice(3))}
        </h3>
      );
      continue;
    }
    if (line.startsWith('# ')) {
      flushList();
      elements.push(
        <h2
          key={`h2-${i}`}
          dir="auto"
          style={{
            fontSize: '1.1875rem',
            fontWeight: 700,
            margin: '14px 0 6px',
            color: 'var(--text-primary)',
          }}
        >
          {renderInlineMarkdown(line.slice(2))}
        </h2>
      );
      continue;
    }

    // Unordered List (- or *)
    const bulletMatch = line.match(/^(\s*)[-*]\s+(.+)/);
    if (bulletMatch) {
      if (isNumberedList) flushList();
      isNumberedList = false;
      currentList.push(bulletMatch[2]);
      continue;
    }

    // Ordered List (1. )
    const numberedMatch = line.match(/^(\s*)\d+\.\s+(.+)/);
    if (numberedMatch) {
      if (!isNumberedList && currentList.length > 0) flushList();
      isNumberedList = true;
      currentList.push(numberedMatch[2]);
      continue;
    }

    // Empty lines flush lists
    if (!line.trim()) {
      flushList();
      continue;
    }

    // Standard paragraph
    flushList();
    elements.push(
      <p
        key={`p-${i}`}
        dir="auto"
        style={{
          margin: '6px 0',
          lineHeight: 1.6,
          wordBreak: 'break-word',
        }}
      >
        {renderInlineMarkdown(line)}
      </p>
    );
  }

  flushList();

  if (inCodeBlock && codeBlockLines.length > 0) {
    const codeText = codeBlockLines.join('\n');
    elements.push(
      <CodeBlockCard
        key={`code-end`}
        code={codeText}
        language={codeBlockLang || 'text'}
      />
    );
  }

  return (
    <div className={`safe-markdown-stream ${className}`} style={style}>
      {elements}
    </div>
  );
};

/**
 * Code Block Card with copy action
 */
const CodeBlockCard: React.FC<{ code: string; language: string }> = ({ code, language }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    try {
      navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Ignore if clipboard access is denied
    }
  };

  return (
    <div
      style={{
        margin: '10px 0',
        borderRadius: 'var(--radius-md)',
        border: '1px solid var(--border-card)',
        background: 'rgba(0, 0, 0, 0.35)',
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '4px 12px',
          background: 'rgba(255, 255, 255, 0.05)',
          borderBottom: '1px solid var(--border-subtle)',
          fontSize: '0.6875rem',
          fontFamily: 'var(--font-mono)',
          color: 'var(--text-muted)',
          textTransform: 'uppercase',
        }}
      >
        <span>{language || 'CODE'}</span>
        <button
          type="button"
          onClick={handleCopy}
          style={{
            background: 'transparent',
            border: 'none',
            color: copied ? 'var(--accent-emerald)' : 'var(--text-secondary)',
            fontSize: '0.6875rem',
            cursor: 'pointer',
            padding: '2px 6px',
            borderRadius: '4px',
          }}
        >
          {copied ? '✓ Copied' : 'Copy'}
        </button>
      </div>
      <pre
        dir="ltr"
        style={{
          margin: 0,
          padding: '12px 14px',
          fontFamily: 'var(--font-mono)',
          fontSize: '0.8125rem',
          lineHeight: 1.5,
          color: 'var(--text-primary)',
          overflowX: 'auto',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        <code>{code}</code>
      </pre>
    </div>
  );
};

/**
 * Parse inline Markdown: bold, italic, inline code, sanitized links
 */
function renderInlineMarkdown(text: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  // Tokenize string with regex for bold, italic, code, links
  // Pattern: links [text](url) | bold **text** | italic *text* | code `text`
  const regex = /(\[.*?\]\(https?:\/\/[^\s)]+\))|(\*\*.*?\*\*)|(\*.*?\*)|(`[^`]+`)/g;

  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }

    const token = match[0];

    // Link: [text](url)
    if (token.startsWith('[') && token.includes('](')) {
      const linkMatch = token.match(/^\[(.*?)\]\((https?:\/\/[^\s)]+)\)$/);
      if (linkMatch) {
        nodes.push(
          <a
            key={`link-${match.index}`}
            href={linkMatch[2]}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              color: 'var(--accent-primary)',
              textDecoration: 'underline',
              wordBreak: 'break-all',
            }}
          >
            {linkMatch[1] || linkMatch[2]}
          </a>
        );
      } else {
        nodes.push(token);
      }
    }
    // Bold: **text**
    else if (token.startsWith('**') && token.endsWith('**')) {
      nodes.push(
        <strong key={`bold-${match.index}`} style={{ fontWeight: 700 }}>
          {token.slice(2, -2)}
        </strong>
      );
    }
    // Italic: *text*
    else if (token.startsWith('*') && token.endsWith('*')) {
      nodes.push(
        <em key={`italic-${match.index}`} style={{ fontStyle: 'italic' }}>
          {token.slice(1, -1)}
        </em>
      );
    }
    // Inline code: `text`
    else if (token.startsWith('`') && token.endsWith('`')) {
      nodes.push(
        <code
          key={`code-${match.index}`}
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: '0.8125rem',
            padding: '2px 6px',
            borderRadius: '4px',
            background: 'rgba(0, 0, 0, 0.25)',
            color: 'var(--accent-cyan)',
            border: '1px solid var(--border-subtle)',
          }}
        >
          {token.slice(1, -1)}
        </code>
      );
    } else {
      nodes.push(token);
    }

    lastIndex = regex.lastIndex;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes;
}

