import React, { useState } from 'react';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { SendIcon } from '../components/common/Icons';

interface MessageItem {
  id: string;
  sender: 'customer' | 'ai';
  text: string;
  timestamp: string;
  language?: string;
  citations?: Array<{ document_code: string; document_title: string; chunk_excerpt: string }>;
  turn?: number;
}

export const ConversationsView: React.FC = () => {
  const [messages, setMessages] = useState<MessageItem[]>([
    {
      id: 'msg-1',
      sender: 'customer',
      text: 'Hello, what is the return policy for electronics if the box was opened?',
      timestamp: '10:14 AM',
      language: 'en',
      turn: 1,
    },
    {
      id: 'msg-2',
      sender: 'ai',
      text: 'Under the US Standard Return Policy (POL-RET-US-2026), opened electronics can be returned within 15 calendar days from delivery, provided all original accessories and packaging are included. A standard 15% restocking fee may apply for opened consumer electronic goods.',
      timestamp: '10:14 AM',
      language: 'en',
      turn: 1,
      citations: [
        {
          document_code: 'POL-RET-US-2026',
          document_title: 'US Standard Return Policy',
          chunk_excerpt: 'Electronics return window is 15 calendar days from physical delivery receipt.',
        },
      ],
    },
  ]);

  const [inputText, setInputText] = useState('');
  const [selectedLang, setSelectedLang] = useState('en');
  const [sessionToken, setSessionToken] = useState('');
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const handleSendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!inputText.trim() || loading) return;

    const query = inputText.trim();
    setInputText('');
    setErrorMsg(null);

    // Add customer message locally
    const custMsg: MessageItem = {
      id: `cust-${Date.now()}`,
      sender: 'customer',
      text: query,
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      language: selectedLang,
    };
    setMessages((prev) => [...prev, custMsg]);
    setLoading(true);

    try {
      // Call actual live backend API POST /api/v1/chat/message
      const res = await fetch('/api/v1/chat/message', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Session-Token': sessionToken,
          'X-Request-ID': `req-${Date.now()}`,
        },
        body: JSON.stringify({
          message: query,
          preferred_language: selectedLang,
          idempotency_key: `idem_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
        }),
      });

      const data = await res.json();
      if (res.ok && data.success && data.data) {
        const payload = data.data;
        const aiMsg: MessageItem = {
          id: payload.message_id || `ai-${Date.now()}`,
          sender: 'ai',
          text: payload.reply_text,
          timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          language: payload.primary_language || payload.detected_language || 'en',
          citations: payload.citations || [],
          turn: payload.turn_sequence_number,
        };
        setMessages((prev) => [...prev, aiMsg]);
      } else {
        const errorDetail = data?.error?.message || data?.detail || 'Failed to communicate with conversational RAG API';
        setErrorMsg(errorDetail);
      }
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Network error connecting to backend API');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '320px 1fr', gap: '24px', height: 'calc(100vh - 120px)' }}>
      {/* Left Conversations Sidebar */}
      <Card variant="base" style={{ display: 'flex', flexDirection: 'column', padding: '16px', overflow: 'hidden' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
          <h2 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>Customer Threads</h2>
          <Badge variant="cyan" size="sm">Live</Badge>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', overflowY: 'auto', flex: 1 }}>
          <div
            style={{
              padding: '12px',
              borderRadius: 'var(--radius-md)',
              background: 'var(--accent-primary-light)',
              border: '1px solid rgba(59, 130, 246, 0.3)',
              cursor: 'pointer',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
              <span style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--text-primary)' }}>John Doe</span>
              <Badge variant="purple" size="sm">VIP</Badge>
            </div>
            <div className="truncate" style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
              What is the return policy for electronics if...
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '8px', fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
              <span>Turn 2 • EN</span>
              <Badge variant="success" size="sm">Satisfied</Badge>
            </div>
          </div>

          <div
            style={{
              padding: '12px',
              borderRadius: 'var(--radius-md)',
              background: 'var(--surface-glass-card)',
              border: 'var(--border-subtle)',
              cursor: 'pointer',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
              <span style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--text-primary)' }}>Carlos Mendez</span>
              <Badge variant="neutral" size="sm">STD</Badge>
            </div>
            <div className="truncate" style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
              ¿Puedo rastrear el envío de mi pedido internacional?
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '8px', fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
              <span>Turn 4 • ES</span>
              <Badge variant="warning" size="sm">Frustrated</Badge>
            </div>
          </div>
        </div>
      </Card>

      {/* Right Active Dialogue Workspace */}
      <Card variant="base" style={{ display: 'flex', flexDirection: 'column', padding: 0, overflow: 'hidden' }}>
        {/* Chat Thread Header */}
        <div
          style={{
            padding: '16px 24px',
            borderBottom: 'var(--border-subtle)',
            background: 'var(--surface-glass-base)',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>John Doe (CUST-DEV-001)</span>
              <Badge variant="purple" size="sm">VIP Tier</Badge>
              <Badge variant="success" size="sm">Live Connected: /api/v1/chat/message</Badge>
            </div>
            <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span>Session Token:</span>
              <input
                type="text"
                placeholder="[REDACTED TEST SESSION TOKEN]"
                value={sessionToken}
                onChange={(e) => setSessionToken(e.target.value)}
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: '0.6875rem',
                  padding: '2px 8px',
                  borderRadius: 'var(--radius-sm)',
                  background: 'var(--surface-glass-input)',
                  border: 'var(--border-card)',
                  color: 'var(--accent-cyan)',
                  width: '220px',
                }}
                title="Active session token (X-Session-Token header for live auth)"
              />
              <span>• Scope: US Standard</span>
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>Language:</span>
            <select
              value={selectedLang}
              onChange={(e) => setSelectedLang(e.target.value)}
              style={{
                padding: '4px 8px',
                borderRadius: 'var(--radius-sm)',
                background: 'var(--surface-glass-input)',
                border: 'var(--border-card)',
                color: 'var(--text-primary)',
                fontSize: '0.75rem',
                outline: 'none',
              }}
            >
              <option value="en">English (EN)</option>
              <option value="es">Español (ES)</option>
              <option value="fr">Français (FR)</option>
              <option value="de">Deutsch (DE)</option>
              <option value="hi">हिन्दी (HI)</option>
            </select>
          </div>
        </div>

        {/* Message History Stream */}
        <div style={{ flex: 1, padding: '24px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {messages.map((msg) => (
            <div
              key={msg.id}
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: msg.sender === 'customer' ? 'flex-end' : 'flex-start',
              }}
            >
              <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', marginBottom: '4px', display: 'flex', gap: '6px' }}>
                <span>{msg.sender === 'customer' ? 'Customer' : 'AI Assistant (RAG Engine)'}</span>
                <span>•</span>
                <span>{msg.timestamp}</span>
                {msg.turn && <span>• Turn #{msg.turn}</span>}
              </div>

              <div
                style={{
                  maxWidth: '75%',
                  padding: '14px 18px',
                  borderRadius: 'var(--radius-lg)',
                  background:
                    msg.sender === 'customer'
                      ? 'var(--accent-primary)'
                      : 'var(--surface-glass-elevated)',
                  color: msg.sender === 'customer' ? '#ffffff' : 'var(--text-primary)',
                  border: msg.sender === 'customer' ? 'none' : 'var(--border-card)',
                  boxShadow: 'var(--shadow-sm)',
                  lineHeight: 1.5,
                  fontSize: '0.875rem',
                }}
              >
                {msg.text}

                {/* Citations block */}
                {msg.citations && msg.citations.length > 0 && (
                  <div style={{ marginTop: '12px', paddingTop: '10px', borderTop: '1px solid rgba(255, 255, 255, 0.1)' }}>
                    <div style={{ fontSize: '0.6875rem', fontWeight: 600, color: 'var(--text-muted)', marginBottom: '4px' }}>
                      GROUNDED CITATIONS:
                    </div>
                    {msg.citations.map((c, i) => (
                      <div
                        key={i}
                        style={{
                          fontSize: '0.75rem',
                          background: 'rgba(0,0,0,0.2)',
                          padding: '6px 10px',
                          borderRadius: 'var(--radius-sm)',
                          display: 'flex',
                          flexDirection: 'column',
                          gap: '2px',
                        }}
                      >
                        <div style={{ fontWeight: 600, color: 'var(--accent-cyan)' }}>
                          [{c.document_code}] {c.document_title}
                        </div>
                        <div style={{ fontSize: '0.6875rem', color: 'var(--text-secondary)' }}>"{c.chunk_excerpt}"</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}

          {errorMsg && (
            <div style={{ padding: '12px', borderRadius: 'var(--radius-md)', background: 'var(--accent-rose-light)', border: '1px solid rgba(244, 63, 94, 0.3)', color: 'var(--accent-rose)', fontSize: '0.8125rem' }}>
              <strong>API Error:</strong> {errorMsg}
            </div>
          )}
        </div>

        {/* Input Form Bar */}
        <form
          onSubmit={handleSendMessage}
          style={{
            padding: '16px 24px',
            borderTop: 'var(--border-subtle)',
            background: 'var(--surface-glass-base)',
            display: 'flex',
            gap: '12px',
          }}
        >
          <input
            type="text"
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            placeholder="Type customer message or policy inquiry (press Enter to send)..."
            disabled={loading}
            style={{
              flex: 1,
              padding: '12px 18px',
              borderRadius: 'var(--radius-full)',
              background: 'var(--surface-glass-input)',
              border: 'var(--border-card)',
              color: 'var(--text-primary)',
              fontSize: '0.875rem',
              outline: 'none',
            }}
          />
          <Button type="submit" loading={loading} icon={<SendIcon size={16} />}>
            Send Turn
          </Button>
        </form>
      </Card>
    </div>
  );
};
