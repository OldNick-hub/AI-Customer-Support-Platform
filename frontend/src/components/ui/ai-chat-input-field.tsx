import React, { useState, useRef, useEffect, useCallback } from 'react';
import { SafeMarkdown } from '../common/SafeMarkdown';

export interface ChatAttachment {
  name: string;
  size: number;
  type: string;
  url?: string;
}

export interface ChatSubmitPayload {
  markdown: string;
  model?: string;
  attachments?: ChatAttachment[];
}

export interface RichMarkdownChatInputProps {
  onSubmit: (payload: ChatSubmitPayload) => void | Promise<void>;
  loading?: boolean;
  onStop?: () => void;
  autoFocus?: boolean;
  placeholder?: string;
  className?: string;
  disabled?: boolean;
  defaultModel?: string;
  supportedModels?: string[];
}

const DEFAULT_MODELS = [
  'Gemini 1.5 Pro (RAG Assistant)',
  'Enterprise Sovereign RAG (70B)',
  'Fast Policy Classifier (Flash)',
];

export const RichMarkdownChatInput: React.FC<RichMarkdownChatInputProps> = ({
  onSubmit,
  loading = false,
  onStop,
  autoFocus = false,
  placeholder = 'Type customer message or policy inquiry in Markdown... (Shift + Enter for new line)',
  className = '',
  disabled = false,
  defaultModel = 'Gemini 1.5 Pro (RAG Assistant)',
  supportedModels = DEFAULT_MODELS,
}) => {
  const [text, setText] = useState('');
  const [activeTab, setActiveTab] = useState<'write' | 'preview'>('write');
  const [selectedModel, setSelectedModel] = useState(defaultModel);
  const [showModelMenu, setShowModelMenu] = useState(false);
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [isFocused, setIsFocused] = useState(false);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Auto-resize textarea height
  const adjustTextareaHeight = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    const nextHeight = Math.min(Math.max(el.scrollHeight, 56), 240);
    el.style.height = `${nextHeight}px`;
  }, []);

  useEffect(() => {
    adjustTextareaHeight();
  }, [text, adjustTextareaHeight]);

  useEffect(() => {
    if (autoFocus && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [autoFocus]);

  // Insert markdown formatting at cursor
  const insertFormatting = (prefix: string, suffix: string = prefix, defaultPlaceholder: string = '') => {
    const el = textareaRef.current;
    if (!el) return;

    const start = el.selectionStart;
    const end = el.selectionEnd;
    const selected = text.substring(start, end) || defaultPlaceholder;
    const replacement = `${prefix}${selected}${suffix}`;

    const newText = text.substring(0, start) + replacement + text.substring(end);
    setText(newText);

    setTimeout(() => {
      el.focus();
      const newCursor = start + prefix.length + selected.length;
      el.setSelectionRange(newCursor, newCursor);
      adjustTextareaHeight();
    }, 0);
  };

  const handleSubmit = () => {
    if (loading || disabled) return;
    const trimmed = text.trim();
    if (!trimmed && attachments.length === 0) return;

    onSubmit({
      markdown: trimmed,
      model: selectedModel,
      attachments: attachments.length > 0 ? attachments : undefined,
    });

    setText('');
    setAttachments([]);
    setActiveTab('write');

    if (textareaRef.current) {
      textareaRef.current.style.height = '56px';
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Escape' && loading && onStop) {
      e.preventDefault();
      onStop();
      return;
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
      return;
    }

    // Ctrl+B: Bold
    if ((e.ctrlKey || e.metaKey) && (e.key === 'b' || e.key === 'B')) {
      e.preventDefault();
      insertFormatting('**', '**', 'bold text');
      return;
    }

    // Ctrl+I: Italic
    if ((e.ctrlKey || e.metaKey) && (e.key === 'i' || e.key === 'I')) {
      e.preventDefault();
      insertFormatting('*', '*', 'italic text');
      return;
    }
  };

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    const newAttachments: ChatAttachment[] = Array.from(files).map((file) => ({
      name: file.name,
      size: file.size,
      type: file.type || 'application/octet-stream',
    }));

    setAttachments((prev) => [...prev, ...newAttachments]);
    e.target.value = '';
  };

  const removeAttachment = (index: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== index));
  };

  const wordCount = text.trim() ? text.trim().split(/\s+/).length : 0;
  const charCount = text.length;

  return (
    <div
      className={`rich-chat-input-container ${className}`}
      style={{
        position: 'relative',
        width: '100%',
        borderRadius: 'var(--radius-2xl)',
        background: 'var(--surface-glass-input)',
        border: `1px solid ${isFocused ? 'var(--border-highlight)' : 'var(--border-card)'}`,
        backdropFilter: 'blur(20px) saturate(180%)',
        WebkitBackdropFilter: 'blur(20px) saturate(180%)',
        boxShadow: isFocused
          ? '0 0 0 2px var(--accent-primary-light), 0 16px 40px -8px rgba(0, 0, 0, 0.45)'
          : '0 8px 32px -4px rgba(0, 0, 0, 0.25)',
        transition: 'all var(--transition-normal)',
        padding: '12px 16px 10px',
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
      }}
    >
      {/* Top Specular Glint */}
      <div
        style={{
          position: 'absolute',
          top: '1px',
          left: '20px',
          right: '20px',
          height: '1px',
          background: 'linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.25), transparent)',
          pointerEvents: 'none',
        }}
      />

      {/* Top Bar: Tabs & Quick Markdown Toolbar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderBottom: '1px solid var(--border-subtle)',
          paddingBottom: '8px',
          flexWrap: 'wrap',
          gap: '8px',
        }}
      >
        {/* Write vs Preview Toggle Chips */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
          <button
            type="button"
            onClick={() => setActiveTab('write')}
            style={{
              padding: '3px 10px',
              borderRadius: 'var(--radius-full)',
              border: 'none',
              background: activeTab === 'write' ? 'var(--surface-glass-elevated)' : 'transparent',
              color: activeTab === 'write' ? 'var(--text-primary)' : 'var(--text-muted)',
              fontSize: '0.75rem',
              fontWeight: 600,
              cursor: 'pointer',
              transition: 'all var(--transition-fast)',
            }}
          >
            Write
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('preview')}
            style={{
              padding: '3px 10px',
              borderRadius: 'var(--radius-full)',
              border: 'none',
              background: activeTab === 'preview' ? 'var(--surface-glass-elevated)' : 'transparent',
              color: activeTab === 'preview' ? 'var(--accent-primary)' : 'var(--text-muted)',
              fontSize: '0.75rem',
              fontWeight: 600,
              cursor: 'pointer',
              transition: 'all var(--transition-fast)',
            }}
          >
            Preview
          </button>
        </div>

        {/* Markdown Action Toolbar */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '3px' }}>
          <button
            type="button"
            onClick={() => insertFormatting('**', '**', 'bold text')}
            title="Bold (Ctrl+B)"
            aria-label="Format Bold"
            className="chat-toolbar-btn"
          >
            <strong>B</strong>
          </button>
          <button
            type="button"
            onClick={() => insertFormatting('*', '*', 'italic text')}
            title="Italic (Ctrl+I)"
            aria-label="Format Italic"
            className="chat-toolbar-btn"
          >
            <em>I</em>
          </button>
          <button
            type="button"
            onClick={() => insertFormatting('`', '`', 'code')}
            title="Inline Code"
            aria-label="Format Inline Code"
            className="chat-toolbar-btn"
          >
            {'</>'}
          </button>
          <button
            type="button"
            onClick={() => insertFormatting('\n```\n', '\n```\n', 'code block')}
            title="Code Block"
            aria-label="Format Code Block"
            className="chat-toolbar-btn"
          >
            {'{ }'}
          </button>
          <button
            type="button"
            onClick={() => insertFormatting('\n- ', '', 'list item')}
            title="Bullet List"
            aria-label="Format List"
            className="chat-toolbar-btn"
          >
            •—
          </button>
          <button
            type="button"
            onClick={() => insertFormatting('[', '](https://example.com)', 'link text')}
            title="Link"
            aria-label="Format Link"
            className="chat-toolbar-btn"
          >
            🔗
          </button>
        </div>
      </div>

      {/* Attachments Chip List */}
      {attachments.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', paddingTop: '4px' }}>
          {attachments.map((att, idx) => (
            <div
              key={idx}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '6px',
                padding: '3px 10px',
                borderRadius: 'var(--radius-full)',
                background: 'var(--surface-glass-hover)',
                border: '1px solid var(--border-subtle)',
                fontSize: '0.6875rem',
                color: 'var(--text-secondary)',
              }}
            >
              <span>📎 {att.name}</span>
              <span style={{ color: 'var(--text-muted)' }}>({Math.round(att.size / 1024)} KB)</span>
              <button
                type="button"
                onClick={() => removeAttachment(idx)}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--accent-rose)',
                  cursor: 'pointer',
                  fontWeight: 700,
                  fontSize: '0.75rem',
                  lineHeight: 1,
                  padding: '0 2px',
                }}
                title="Remove attachment"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Main Content Area: Write Textarea OR Markdown Preview */}
      {activeTab === 'write' ? (
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => setIsFocused(true)}
          onBlur={() => setIsFocused(false)}
          placeholder={placeholder}
          disabled={disabled || loading}
          rows={2}
          dir="auto"
          style={{
            width: '100%',
            minHeight: '56px',
            maxHeight: '240px',
            resize: 'none',
            background: 'transparent',
            border: 'none',
            outline: 'none',
            color: 'var(--text-primary)',
            fontSize: '0.9375rem',
            lineHeight: 1.6,
            fontFamily: 'var(--font-sans)',
            padding: '4px 0',
          }}
        />
      ) : (
        <div
          dir="auto"
          style={{
            minHeight: '56px',
            maxHeight: '240px',
            overflowY: 'auto',
            padding: '4px 0',
            fontSize: '0.9375rem',
            color: 'var(--text-primary)',
          }}
        >
          {text.trim() ? (
            <SafeMarkdown content={text} />
          ) : (
            <span style={{ color: 'var(--text-muted)', fontStyle: 'italic', fontSize: '0.875rem' }}>
              Nothing to preview yet. Type markdown in the Write tab.
            </span>
          )}
        </div>
      )}

      {/* Bottom Bar: Model Selector, Metadata & Submit / Stop Buttons */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderTop: '1px solid var(--border-subtle)',
          paddingTop: '8px',
          gap: '12px',
          flexWrap: 'wrap',
        }}
      >
        {/* Left Controls: File Attachment & Model Dropdown */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', position: 'relative' }}>
          {/* File Upload Button */}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            onChange={handleFileUpload}
            style={{ display: 'none' }}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            title="Attach Evidence Document or Screenshot"
            aria-label="Attach File"
            className="chat-icon-btn"
          >
            📎
          </button>

          {/* Model Selector Pill */}
          <button
            type="button"
            onClick={() => setShowModelMenu(!showModelMenu)}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: '6px',
              padding: '4px 10px',
              borderRadius: 'var(--radius-full)',
              background: 'var(--surface-glass-base)',
              border: '1px solid var(--border-subtle)',
              color: 'var(--text-secondary)',
              fontSize: '0.6875rem',
              fontWeight: 600,
              cursor: 'pointer',
              transition: 'all var(--transition-fast)',
            }}
          >
            <span style={{ color: 'var(--accent-cyan)' }}>✦</span>
            <span>{selectedModel}</span>
            <span style={{ fontSize: '0.5625rem', opacity: 0.6 }}>▼</span>
          </button>

          {/* Model Dropdown Menu */}
          {showModelMenu && (
            <div
              style={{
                position: 'absolute',
                bottom: '36px',
                left: 0,
                zIndex: 50,
                background: 'var(--surface-glass-card)',
                backdropFilter: 'blur(20px)',
                WebkitBackdropFilter: 'blur(20px)',
                border: '1px solid var(--border-card)',
                borderRadius: 'var(--radius-md)',
                boxShadow: 'var(--shadow-lg)',
                padding: '4px',
                minWidth: '240px',
                display: 'flex',
                flexDirection: 'column',
                gap: '2px',
              }}
            >
              {supportedModels.map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => {
                    setSelectedModel(m);
                    setShowModelMenu(false);
                  }}
                  style={{
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-sm)',
                    border: 'none',
                    background: selectedModel === m ? 'var(--accent-primary-light)' : 'transparent',
                    color: selectedModel === m ? 'var(--accent-primary)' : 'var(--text-primary)',
                    fontSize: '0.75rem',
                    textAlign: 'left',
                    cursor: 'pointer',
                    fontWeight: selectedModel === m ? 600 : 400,
                  }}
                >
                  {m}
                </button>
              ))}
            </div>
          )}

          {/* Word / Character Counter */}
          <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
            {charCount} chars · {wordCount} words
          </span>
        </div>

        {/* Right Controls: Submit OR Stop Button */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <span
            style={{
              fontSize: '0.6875rem',
              color: 'var(--text-muted)',
              display: 'none',
            }}
            className="shortcut-hint"
          >
            ↵ to send
          </span>

          {loading ? (
            /* Animated Stop Button */
            <button
              type="button"
              onClick={onStop}
              title="Stop generation (Esc)"
              aria-label="Stop generation"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '6px',
                padding: '6px 14px',
                borderRadius: 'var(--radius-full)',
                background: 'rgba(244, 63, 94, 0.15)',
                border: '1px solid rgba(244, 63, 94, 0.4)',
                color: 'var(--accent-rose)',
                fontSize: '0.75rem',
                fontWeight: 600,
                cursor: 'pointer',
                boxShadow: '0 0 16px rgba(244, 63, 94, 0.35)',
                transition: 'all var(--transition-fast)',
              }}
            >
              <span
                style={{
                  width: '8px',
                  height: '8px',
                  background: 'var(--accent-rose)',
                  borderRadius: '2px',
                }}
              />
              <span>Stop</span>
            </button>
          ) : (
            /* Send Button */
            <button
              type="button"
              onClick={handleSubmit}
              disabled={disabled || (!text.trim() && attachments.length === 0)}
              title="Send message (Enter)"
              aria-label="Send message"
              style={{
                width: '36px',
                height: '36px',
                borderRadius: '50%',
                border: 'none',
                background:
                  text.trim() || attachments.length > 0
                    ? 'linear-gradient(135deg, var(--accent-primary), var(--accent-cyan))'
                    : 'var(--surface-glass-elevated)',
                color: text.trim() || attachments.length > 0 ? '#ffffff' : 'var(--text-muted)',
                cursor: text.trim() || attachments.length > 0 ? 'pointer' : 'not-allowed',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                boxShadow:
                  text.trim() || attachments.length > 0 ? '0 0 16px var(--accent-primary-light)' : 'none',
                transition: 'all var(--transition-fast)',
              }}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="12" y1="19" x2="12" y2="5" />
                <polyline points="5 12 12 5 19 12" />
              </svg>
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export default RichMarkdownChatInput;

