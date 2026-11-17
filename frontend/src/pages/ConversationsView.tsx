import React, { useState, useRef, useEffect, useCallback } from 'react';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { SafeMarkdown } from '../components/common/SafeMarkdown';
import { RichMarkdownChatInput, type ChatSubmitPayload } from '../components/ui/ai-chat-input-field';
import { ParticleTitle } from '../components/ui/particle-title';
import {
  conversationsApi,
  chatApi,
  ticketsApi,
  type ConversationListItem,
  type CitationDTO,
  type TicketListItem,
  type SynthesisDetailResponse,
  type ClarificationListItem,
} from '../services/api';
import { useAuth } from '../context/AuthContext';

interface DisplayMessage {
  id: string;
  sender_type: 'CUSTOMER' | 'AI_AGENT' | 'HUMAN_AGENT' | 'SYSTEM';
  content: string;
  created_at: string;
  turn_sequence_number?: number | null;
  language?: string;
  citations?: CitationDTO[];
  is_escalated?: boolean;
}

const EditIcon: React.FC<{ size?: number }> = ({ size = 13 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
    <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
  </svg>
);

const TrashIcon: React.FC<{ size?: number }> = ({ size = 13 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M3 6h18" />
    <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" />
    <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
  </svg>
);

const STARTER_SUGGESTIONS = [
  {
    icon: '📦',
    title: 'Track an Order',
    description: 'Carrier status & live ETA',
    prompt: 'Can you check the delivery status of my order ORD-5544?',
  },
  {
    icon: '💔',
    title: 'Report Damage',
    description: 'Replace broken or unboxed goods',
    prompt: 'I received order ORD-9812 today but the box was damaged and the screen is cracked. What should I do?',
  },
  {
    icon: '🔄',
    title: 'Return & Refund',
    description: '30-day window & instant RMA',
    prompt: 'What is the return window and refund policy for electronics?',
  },
  {
    icon: '🌐',
    title: 'Multilingual Help',
    description: 'Hindi, Hinglish & 95+ languages',
    prompt: 'Mera package delay ho gaya hai, order ORD-5544 abhi tak nahi mila. Kripya check kijiye.',
  },
];

export const ConversationsView: React.FC = () => {
  const { activeSession, currentUser } = useAuth();

  // Conversation List State
  const [conversations, setConversations] = useState<ConversationListItem[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const [isLoadingConversations, setIsLoadingConversations] = useState<boolean>(true);
  const [conversationsError, setConversationsError] = useState<string | null>(null);

  // Thread Renaming & Delete State
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState<string>('');
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState<boolean>(false);
  const [threadSearch, setThreadSearch] = useState<string>('');
  const [localTitles, setLocalTitles] = useState<Record<string, string>>(() => {
    try {
      const saved = localStorage.getItem('thread_custom_titles');
      return saved ? JSON.parse(saved) : {};
    } catch {
      return {};
    }
  });

  // Active Thread Messages State
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [isLoadingMessages, setIsLoadingMessages] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [sendingMessage, setSendingMessage] = useState<boolean>(false);

  // Settings & Controls
  const [selectedLang, setSelectedLang] = useState<string>('en');

  // Ticket Synthesis & Clarification State
  const [linkedTicket, setLinkedTicket] = useState<TicketListItem | null>(null);
  const [synthesisData, setSynthesisData] = useState<SynthesisDetailResponse | null>(null);
  const [clarifications, setClarifications] = useState<ClarificationListItem[]>([]);
  const [isSynthesisOpen, setIsSynthesisOpen] = useState<boolean>(false);
  const [isSynthesizing, setIsSynthesizing] = useState<boolean>(false);
  const [clarificationInput, setClarificationInput] = useState<string>('');
  const [submittingClarification, setSubmittingClarification] = useState<boolean>(false);
  const [synthesisActionMsg, setSynthesisActionMsg] = useState<string | null>(null);

  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  // Check if current user is staff
  const isStaff =
    activeSession?.auth_type === 'JWT' ||
    (currentUser?.roles || activeSession?.roles || []).some((r) =>
      ['AGENT', 'SUPERVISOR', 'ADMIN'].includes(r.toUpperCase())
    );

  // Auto-scroll messages to bottom
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // Load Synthesis and Clarification Data
  const loadSynthesis = useCallback(async (ticketId: string) => {
    try {
      const syn = await ticketsApi.getSynthesis(ticketId);
      setSynthesisData(syn);
    } catch {
      setSynthesisData(null);
    }

    try {
      const clarRes = await ticketsApi.getClarifications(ticketId);
      setClarifications(clarRes.rounds || []);
    } catch {
      setClarifications([]);
    }
  }, []);

  // Lookup linked ticket for active conversation
  const checkLinkedTicket = useCallback(
    async (convId: string) => {
      try {
        const ticketList = await ticketsApi.listTickets({ limit: 100 });
        const match = ticketList.items.find((t) => t.conversation_id === convId);
        if (match) {
          setLinkedTicket(match);
          await loadSynthesis(match.id);
        } else {
          setLinkedTicket(null);
          setSynthesisData(null);
          setClarifications([]);
        }
      } catch {
        setLinkedTicket(null);
        setSynthesisData(null);
        setClarifications([]);
      }
    },
    [loadSynthesis]
  );

  // Load Messages for a specific conversation
  const loadMessages = useCallback(
    async (convId: string) => {
      setIsLoadingMessages(true);
      setErrorMsg(null);
      try {
        const res = await conversationsApi.getConversationMessages(convId);
        const mapped: DisplayMessage[] = res.items.map((m) => {
          let timeDisplay = m.created_at;
          try {
            timeDisplay = new Date(m.created_at).toLocaleTimeString([], {
              hour: '2-digit',
              minute: '2-digit',
            });
          } catch {
            // Keep original string if date parse fails
          }

          let normalizedSender: DisplayMessage['sender_type'] = 'CUSTOMER';
          if (m.sender_type === 'AI_AGENT' || m.sender_type === 'AI') {
            normalizedSender = 'AI_AGENT';
          } else if (m.sender_type === 'HUMAN_AGENT' || m.sender_type === 'AGENT') {
            normalizedSender = 'HUMAN_AGENT';
          } else if (m.sender_type === 'SYSTEM') {
            normalizedSender = 'SYSTEM';
          }

          return {
            id: m.id,
            sender_type: normalizedSender,
            content: m.content,
            created_at: timeDisplay,
            turn_sequence_number: m.turn_sequence_number,
            language: m.detected_language,
          };
        });

        setMessages(mapped);
        await checkLinkedTicket(convId);
      } catch (err: any) {
        const msg =
          err?.response?.data?.error?.message ||
          err?.response?.data?.detail ||
          'Failed to load conversation messages.';
        setErrorMsg(msg);
      } finally {
        setIsLoadingMessages(false);
      }
    },
    [checkLinkedTicket]
  );

  // Fetch conversations list
  const fetchConversations = useCallback(
    async (preserveSelectedId?: string | null) => {
      setIsLoadingConversations(true);
      setConversationsError(null);
      try {
        const res = await conversationsApi.listConversations({ limit: 50 });
        setConversations(res.items);

        // Determine which conversation to select
        const targetId = preserveSelectedId ?? selectedConversationId;
        if (targetId && res.items.some((c) => c.id === targetId)) {
          setSelectedConversationId(targetId);
          loadMessages(targetId);
        } else if (res.items.length > 0 && targetId === undefined) {
          const firstId = res.items[0].id;
          setSelectedConversationId(firstId);
          loadMessages(firstId);
        }
      } catch (err: any) {
        const msg =
          err?.response?.data?.error?.message ||
          err?.response?.data?.detail ||
          'Failed to retrieve conversations.';
        setConversationsError(msg);
      } finally {
        setIsLoadingConversations(false);
      }
    },
    [loadMessages, selectedConversationId]
  );

  // Reload conversations when active session changes
  useEffect(() => {
    fetchConversations();
  }, [activeSession?.token]);

  // Handle selecting a conversation from the sidebar
  const handleSelectConversation = (convId: string) => {
    if (convId === selectedConversationId) return;
    setSelectedConversationId(convId);
    loadMessages(convId);
  };

  // Start new conversation thread
  const handleNewThread = () => {
    setSelectedConversationId(null);
    setMessages([]);
    setLinkedTicket(null);
    setSynthesisData(null);
    setClarifications([]);
    setIsSynthesisOpen(false);
    setErrorMsg(null);
  };

  // Helper to resolve readable thread title
  const getThreadTitle = useCallback(
    (conv: ConversationListItem): string => {
      if (conv.custom_title) return conv.custom_title;
      if (localTitles[conv.id]) return localTitles[conv.id];
      const shortId = conv.id.substring(0, 6).toUpperCase();
      if (conv.is_escalated) return `Escalated #${shortId}`;
      if (conv.status === 'RESOLVED') return `Resolved #${shortId}`;
      return `Inquiry #${shortId}`;
    },
    [localTitles]
  );

  const handleStartRename = (conv: ConversationListItem, e: React.MouseEvent) => {
    e.stopPropagation();
    setRenamingId(conv.id);
    setRenameValue(getThreadTitle(conv));
    setDeleteConfirmId(null);
  };

  const handleSaveRename = async (
    convId: string,
    e?: React.FormEvent | React.MouseEvent | React.KeyboardEvent
  ) => {
    if (e) e.stopPropagation();
    const trimmed = renameValue.trim();
    if (!trimmed) {
      setRenamingId(null);
      return;
    }
    const updatedLocal = { ...localTitles, [convId]: trimmed };
    setLocalTitles(updatedLocal);
    try {
      localStorage.setItem('thread_custom_titles', JSON.stringify(updatedLocal));
    } catch {}

    setConversations((prev) =>
      prev.map((c) => (c.id === convId ? { ...c, custom_title: trimmed } : c))
    );
    setRenamingId(null);

    try {
      await conversationsApi.renameConversation(convId, trimmed);
    } catch (err) {
      console.warn('Backend rename failed, preserved locally:', err);
    }
  };

  const handleCancelRename = (e: React.MouseEvent) => {
    e.stopPropagation();
    setRenamingId(null);
  };

  const handlePromptDelete = (convId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setDeleteConfirmId(convId);
    setRenamingId(null);
  };

  const handleExecuteDelete = async (convId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setIsDeleting(true);
    try {
      await conversationsApi.deleteConversation(convId);
      setConversations((prev) => prev.filter((c) => c.id !== convId));
      const updatedLocal = { ...localTitles };
      delete updatedLocal[convId];
      setLocalTitles(updatedLocal);
      try {
        localStorage.setItem('thread_custom_titles', JSON.stringify(updatedLocal));
      } catch {}

      if (selectedConversationId === convId) {
        handleNewThread();
      }
      setDeleteConfirmId(null);
    } catch (err: any) {
      const msg = err?.response?.data?.detail || 'Failed to delete thread.';
      setErrorMsg(msg);
    } finally {
      setIsDeleting(false);
    }
  };

  const handleCancelDelete = (e: React.MouseEvent) => {
    e.stopPropagation();
    setDeleteConfirmId(null);
  };

  const handleSelectStarter = (promptText: string) => {
    handleSendMessage({ markdown: promptText });
  };

  // Abort in-flight query
  const handleStop = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setSendingMessage(false);
  };

  // Send message
  const handleSendMessage = async (payload: ChatSubmitPayload) => {
    const query = payload.markdown.trim();
    if (!query || sendingMessage) return;

    setErrorMsg(null);

    // Optimistic customer message
    const tempId = `temp-${Date.now()}`;
    const custMsg: DisplayMessage = {
      id: tempId,
      sender_type: 'CUSTOMER',
      content: query,
      created_at: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      language: selectedLang,
    };
    setMessages((prev) => [...prev, custMsg]);
    setSendingMessage(true);

    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      const idempotencyKey = `idem_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`;
      const res = await chatApi.sendMessage(
        {
          message: query,
          conversation_id: selectedConversationId || undefined,
          preferred_language: selectedLang,
          idempotency_key: idempotencyKey,
        },
        controller.signal
      );

      if (res.success && res.data) {
        const d = res.data;
        const aiMsg: DisplayMessage = {
          id: d.message_id || `ai-${Date.now()}`,
          sender_type: 'AI_AGENT',
          content: d.reply_text,
          created_at: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          language: d.primary_language || d.detected_language || selectedLang,
          citations: d.citations || [],
          turn_sequence_number: d.turn_sequence_number,
          is_escalated: d.is_escalated,
        };
        setMessages((prev) => [...prev, aiMsg]);

        // If this was a new thread, update conversation ID and refresh list
        if (!selectedConversationId && d.conversation_id) {
          setSelectedConversationId(d.conversation_id);
          fetchConversations(d.conversation_id);
        }
      } else {
        setErrorMsg('Unable to process your message right now. Please try again.');
      }
    } catch (err: any) {
      if (err?.name === 'AbortError' || err?.code === 'ERR_CANCELED') {
        console.info('Query cancelled by user.');
      } else {
        const errorDetail =
          err?.response?.data?.error?.message ||
          err?.response?.data?.detail ||
          err?.message ||
          'Unable to connect to support. Please check your connection and try again.';
        setErrorMsg(errorDetail);
      }
    } finally {
      abortControllerRef.current = null;
      setSendingMessage(false);
    }
  };

  // Trigger Ticket Synthesis
  const handleTriggerSynthesis = async () => {
    if (!linkedTicket) return;
    setIsSynthesizing(true);
    setSynthesisActionMsg(null);
    try {
      await ticketsApi.synthesizeTicket(linkedTicket.id, true);
      setSynthesisActionMsg('Synthesis completed successfully.');
      await loadSynthesis(linkedTicket.id);
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to execute ticket synthesis.';
      setSynthesisActionMsg(`Synthesis error: ${msg}`);
    } finally {
      setIsSynthesizing(false);
    }
  };

  // Submit Clarification Response
  const handleSubmitClarification = async (clarificationRequestId: string) => {
    if (!linkedTicket || !clarificationInput.trim()) return;
    setSubmittingClarification(true);
    setSynthesisActionMsg(null);
    try {
      const res = await ticketsApi.respondToClarification(
        linkedTicket.id,
        clarificationRequestId,
        clarificationInput.trim()
      );
      setClarificationInput('');
      setSynthesisActionMsg(
        res.response_absorbed
          ? 'Clarification response absorbed successfully.'
          : 'Response recorded but clarification remains ambiguous.'
      );
      await loadSynthesis(linkedTicket.id);
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to submit clarification response.';
      setSynthesisActionMsg(`Clarification error: ${msg}`);
    } finally {
      setSubmittingClarification(false);
    }
  };

  // Mark Synthesis Complete (Staff only)
  const handleMarkComplete = async () => {
    if (!linkedTicket) return;
    setIsSynthesizing(true);
    setSynthesisActionMsg(null);
    try {
      await ticketsApi.markSynthesisComplete(linkedTicket.id);
      setSynthesisActionMsg('Ticket synthesis marked complete.');
      await loadSynthesis(linkedTicket.id);
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to mark synthesis complete.';
      setSynthesisActionMsg(`Complete error: ${msg}`);
    } finally {
      setIsSynthesizing(false);
    }
  };

  // Selected conversation object
  const activeConversation = conversations.find((c) => c.id === selectedConversationId);

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: isSynthesisOpen ? '280px 1fr 340px' : '300px 1fr',
        gap: '20px',
        height: 'calc(100vh - 120px)',
        transition: 'grid-template-columns 0.2s ease',
      }}
    >
      {/* =========================================================================
          COLUMN 1: Conversations Sidebar
      ========================================================================= */}
      <Card
        variant="base"
        style={{ display: 'flex', flexDirection: 'column', padding: '16px', overflow: 'hidden' }}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: '14px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <h2 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>
              Threads
            </h2>
            <Badge variant="cyan" size="sm">
              Live
            </Badge>
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={handleNewThread}
            style={{ fontSize: '0.75rem', padding: '4px 8px' }}
          >
            + New
          </Button>
        </div>

        {/* Thread Search Box */}
        <div style={{ marginBottom: '10px' }}>
          <input
            type="text"
            placeholder="Search threads..."
            value={threadSearch}
            onChange={(e) => setThreadSearch(e.target.value)}
            style={{
              width: '100%',
              padding: '6px 10px',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--surface-glass-input)',
              border: '1px solid var(--border-subtle)',
              color: 'var(--text-primary)',
              fontSize: '0.75rem',
              outline: 'none',
              boxSizing: 'border-box',
            }}
          />
        </div>

        {/* Conversation List Stream */}
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: '8px',
            overflowY: 'auto',
            flex: 1,
            paddingRight: '4px',
          }}
        >
          {isLoadingConversations && conversations.length === 0 ? (
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                gap: '8px',
                padding: '12px 0',
              }}
            >
              {[1, 2, 3].map((n) => (
                <div
                  key={n}
                  style={{
                    height: '64px',
                    borderRadius: 'var(--radius-md)',
                    background: 'var(--surface-glass-card)',
                    opacity: 0.6,
                    animation: 'pulse 1.5s infinite ease-in-out',
                  }}
                />
              ))}
            </div>
          ) : conversationsError ? (
            <div
              style={{
                padding: '12px',
                borderRadius: 'var(--radius-md)',
                background: 'var(--accent-rose-light)',
                color: 'var(--accent-rose)',
                fontSize: '0.75rem',
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: '4px' }}>Load Error</div>
              <div>{conversationsError}</div>
              <Button
                size="sm"
                variant="outline"
                onClick={() => fetchConversations()}
                style={{ marginTop: '8px', width: '100%' }}
              >
                Retry
              </Button>
            </div>
          ) : conversations.length === 0 ? (
            <div
              style={{
                padding: '24px 12px',
                textAlign: 'center',
                color: 'var(--text-muted)',
                fontSize: '0.8125rem',
              }}
            >
              <p>No conversations found.</p>
              <Button
                size="sm"
                variant="primary"
                onClick={handleNewThread}
                style={{ marginTop: '12px' }}
              >
                Start New Thread
              </Button>
            </div>
          ) : (
            conversations
              .filter((c) => {
                if (!threadSearch.trim()) return true;
                const q = threadSearch.toLowerCase();
                return (
                  getThreadTitle(c).toLowerCase().includes(q) ||
                  c.id.toLowerCase().includes(q) ||
                  c.status.toLowerCase().includes(q)
                );
              })
              .map((conv) => {
              const isSelected = conv.id === selectedConversationId;
              const isRenaming = renamingId === conv.id;
              const isConfirmingDelete = deleteConfirmId === conv.id;
              const threadTitle = getThreadTitle(conv);

              let dateStr = conv.last_message_at || conv.started_at;
              try {
                dateStr = new Date(dateStr).toLocaleDateString([], {
                  month: 'short',
                  day: 'numeric',
                });
              } catch {
                // Keep raw string fallback
              }

              return (
                <div
                  key={conv.id}
                  onClick={() => handleSelectConversation(conv.id)}
                  style={{
                    padding: '12px',
                    borderRadius: 'var(--radius-md)',
                    background: isSelected
                      ? 'var(--accent-primary-light)'
                      : 'var(--surface-glass-card)',
                    border: isSelected
                      ? '1px solid rgba(59, 130, 246, 0.4)'
                      : 'var(--border-subtle)',
                    cursor: 'pointer',
                    transition: 'all var(--transition-fast)',
                  }}
                >
                  {isRenaming ? (
                    <div
                      onClick={(e) => e.stopPropagation()}
                      style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}
                    >
                      <input
                        type="text"
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') handleSaveRename(conv.id, e);
                          if (e.key === 'Escape') handleCancelRename(e as any);
                        }}
                        autoFocus
                        style={{
                          padding: '6px 8px',
                          borderRadius: 'var(--radius-sm)',
                          border: '1px solid var(--accent-primary)',
                          background: 'var(--surface-glass-input)',
                          color: 'var(--text-primary)',
                          fontSize: '0.8125rem',
                          outline: 'none',
                          width: '100%',
                        }}
                      />
                      <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={handleCancelRename}
                          style={{ padding: '2px 8px', fontSize: '0.75rem' }}
                        >
                          Cancel
                        </Button>
                        <Button
                          size="sm"
                          variant="primary"
                          onClick={(e) => handleSaveRename(conv.id, e)}
                          style={{ padding: '2px 8px', fontSize: '0.75rem' }}
                        >
                          Save
                        </Button>
                      </div>
                    </div>
                  ) : isConfirmingDelete ? (
                    <div
                      onClick={(e) => e.stopPropagation()}
                      style={{
                        padding: '4px 0',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: '6px',
                      }}
                    >
                      <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--accent-rose)' }}>
                        Delete this thread?
                      </div>
                      <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={handleCancelDelete}
                          disabled={isDeleting}
                          style={{ padding: '2px 8px', fontSize: '0.75rem' }}
                        >
                          Cancel
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={(e) => handleExecuteDelete(conv.id, e)}
                          disabled={isDeleting}
                          style={{ padding: '2px 8px', fontSize: '0.75rem' }}
                        >
                          {isDeleting ? 'Deleting...' : 'Delete'}
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                          marginBottom: '6px',
                          gap: '6px',
                        }}
                      >
                        <span
                          style={{
                            fontSize: '0.8125rem',
                            fontWeight: 600,
                            color: isSelected ? 'var(--accent-primary)' : 'var(--text-primary)',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                            flex: 1,
                          }}
                          title={threadTitle}
                        >
                          {threadTitle}
                        </span>

                        <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                          <button
                            onClick={(e) => handleStartRename(conv, e)}
                            style={{
                              background: 'rgba(59, 130, 246, 0.08)',
                              border: '1px solid rgba(59, 130, 246, 0.25)',
                              color: '#60a5fa',
                              cursor: 'pointer',
                              padding: '4px 7px',
                              borderRadius: 'var(--radius-sm)',
                              display: 'inline-flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              transition: 'all 0.15s ease',
                            }}
                            onMouseEnter={(e) => (e.currentTarget.style.background = 'rgba(59, 130, 246, 0.2)')}
                            onMouseLeave={(e) => (e.currentTarget.style.background = 'rgba(59, 130, 246, 0.08)')}
                            title="Rename thread"
                            aria-label="Rename thread"
                          >
                            <EditIcon size={12} />
                          </button>
                          <button
                            onClick={(e) => handlePromptDelete(conv.id, e)}
                            style={{
                              background: 'rgba(239, 68, 68, 0.08)',
                              border: '1px solid rgba(239, 68, 68, 0.25)',
                              color: '#f87171',
                              cursor: 'pointer',
                              padding: '4px 7px',
                              borderRadius: 'var(--radius-sm)',
                              display: 'inline-flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              transition: 'all 0.15s ease',
                            }}
                            onMouseEnter={(e) => (e.currentTarget.style.background = 'rgba(239, 68, 68, 0.2)')}
                            onMouseLeave={(e) => (e.currentTarget.style.background = 'rgba(239, 68, 68, 0.08)')}
                            title="Delete thread"
                            aria-label="Delete thread"
                          >
                            <TrashIcon size={12} />
                          </button>
                          <Badge
                            variant={conv.status === 'ACTIVE' || conv.status === 'OPEN' ? 'success' : conv.is_escalated ? 'danger' : 'neutral'}
                            size="sm"
                          >
                            {conv.status}
                          </Badge>
                        </div>
                      </div>

                      <div
                        style={{
                          fontSize: '0.75rem',
                          color: 'var(--text-secondary)',
                          display: 'flex',
                          gap: '6px',
                          alignItems: 'center',
                        }}
                      >
                        <span>
                          {conv.turn_count} {conv.turn_count === 1 ? 'message' : 'messages'}
                        </span>
                        <span>•</span>
                        <span>{conv.primary_language.toUpperCase()}</span>
                        <span>•</span>
                        <span>{dateStr}</span>
                      </div>

                      {conv.is_escalated && (
                        <div style={{ marginTop: '6px' }}>
                          <Badge variant="danger" size="sm">
                            Escalated
                          </Badge>
                        </div>
                      )}
                    </>
                  )}
                </div>
              );
            })
          )}
        </div>
      </Card>

      {/* =========================================================================
          COLUMN 2: Active Dialogue Workspace
      ========================================================================= */}
      <Card
        variant="base"
        style={{ display: 'flex', flexDirection: 'column', padding: 0, overflow: 'hidden' }}
      >
        {/* Workspace Header */}
        <div
          style={{
            padding: '14px 20px',
            borderBottom: 'var(--border-subtle)',
            background: 'var(--surface-glass-base)',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            flexWrap: 'wrap',
            gap: '12px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <span
                  style={{
                    fontSize: '0.9375rem',
                    fontWeight: 700,
                    color: 'var(--text-primary)',
                  }}
                >
                  {activeConversation ? getThreadTitle(activeConversation) : 'New Conversation'}
                </span>
                {activeConversation && (
                  <button
                    onClick={(e) => handleStartRename(activeConversation, e)}
                    style={{
                      background: 'rgba(255, 255, 255, 0.05)',
                      border: '1px solid var(--border-subtle)',
                      color: 'var(--text-muted)',
                      cursor: 'pointer',
                      padding: '3px 6px',
                      borderRadius: 'var(--radius-sm)',
                      display: 'inline-flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                    }}
                    title="Rename this thread"
                    aria-label="Rename this thread"
                  >
                    <EditIcon size={12} />
                  </button>
                )}
                {activeConversation && (
                  <button
                    onClick={(e) => handlePromptDelete(activeConversation.id, e)}
                    style={{
                      background: 'rgba(255, 255, 255, 0.05)',
                      border: '1px solid var(--border-subtle)',
                      color: 'var(--accent-rose)',
                      cursor: 'pointer',
                      padding: '3px 6px',
                      borderRadius: 'var(--radius-sm)',
                      display: 'inline-flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                    }}
                    title="Delete this thread"
                    aria-label="Delete this thread"
                  >
                    <TrashIcon size={12} />
                  </button>
                )}
                {activeConversation && (
                  <Badge variant="cyan" size="sm">
                    {activeConversation.status}
                  </Badge>
                )}
                {linkedTicket && (
                  <Badge variant="purple" size="sm">
                    Ticket #{linkedTicket.ticket_number}
                  </Badge>
                )}
              </div>
              <div
                style={{
                  fontSize: '0.75rem',
                  color: 'var(--text-muted)',
                  marginTop: '2px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                }}
              >
                <span>Session:</span>
                <span style={{ fontWeight: 600, color: 'var(--text-secondary)' }}>
                  {activeSession?.display_name || 'Active Session'}
                </span>
              </div>
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            {/* Language Selector */}
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
              title="Preferred language for natural language response"
            >
              <option value="en">English (EN)</option>
              <option value="es">Español (ES)</option>
              <option value="fr">Français (FR)</option>
              <option value="de">Deutsch (DE)</option>
              <option value="hi">हिन्दी (HI)</option>
            </select>

            {/* Ticket Synthesis Drawer Toggle */}
            <Button
              size="sm"
              variant={isSynthesisOpen ? 'primary' : 'secondary'}
              onClick={() => setIsSynthesisOpen((prev) => !prev)}
              style={{ fontSize: '0.75rem', padding: '4px 10px' }}
            >
              {isSynthesisOpen ? 'Hide Synthesis' : 'Ticket Synthesis'}
              {synthesisData && (
                <Badge
                  variant={synthesisData.completeness_score >= 0.8 ? 'success' : 'warning'}
                  size="sm"
                  style={{ marginLeft: '6px' }}
                >
                  {Math.round(synthesisData.completeness_score * 100)}%
                </Badge>
              )}
            </Button>

            {/* Refresh Messages Button */}
            {selectedConversationId && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => loadMessages(selectedConversationId)}
                title="Refresh messages"
                style={{ padding: '4px 8px' }}
              >
                ↻
              </Button>
            )}
          </div>
        </div>

        {/* Message Stream */}
        <div
          style={{
            flex: 1,
            padding: '20px',
            overflowY: 'auto',
            display: 'flex',
            flexDirection: 'column',
            gap: '16px',
          }}
        >
          {isLoadingMessages ? (
            <div
              style={{
                display: 'flex',
                justifyContent: 'center',
                alignItems: 'center',
                flex: 1,
                color: 'var(--text-muted)',
                fontSize: '0.875rem',
              }}
            >
              Loading conversation messages...
            </div>
          ) : messages.length === 0 ? (
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                flex: 1,
                padding: '16px 20px',
                maxWidth: '720px',
                margin: '0 auto',
                width: '100%',
              }}
            >
              {/* Compact Pill Badge */}
              <div
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '6px',
                  padding: '3px 12px',
                  borderRadius: '9999px',
                  background: 'rgba(56, 189, 248, 0.1)',
                  border: '1px solid rgba(56, 189, 248, 0.25)',
                  fontSize: '0.6875rem',
                  fontWeight: 600,
                  letterSpacing: '0.04em',
                  color: '#38bdf8',
                  marginBottom: '6px',
                }}
              >
                <span>✨</span>
                <span>Antigravity AI Copilot • Multi-Turn RAG</span>
              </div>

              {/* Luminous Particle Title */}
              <div style={{ width: '100%', maxWidth: '640px', margin: '0 auto 6px auto' }}>
                <ParticleTitle
                  text="How can we help you today?"
                  as="h2"
                  height={80}
                  showHint={false}
                />
              </div>

              <p
                style={{
                  fontSize: '0.8125rem',
                  color: 'var(--text-secondary)',
                  textAlign: 'center',
                  maxWidth: '520px',
                  marginBottom: '16px',
                  lineHeight: '1.4',
                }}
              >
                Select a scenario below to start an interactive test session, or ask a question in any language.
              </p>

              {/* Compact Quick Starter Grid */}
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(2, 1fr)',
                  gap: '10px',
                  width: '100%',
                  marginBottom: '16px',
                }}
              >
                {STARTER_SUGGESTIONS.map((item, idx) => (
                  <div
                    key={idx}
                    onClick={() => handleSelectStarter(item.prompt)}
                    style={{
                      padding: '10px 14px',
                      borderRadius: 'var(--radius-md)',
                      background: 'var(--surface-glass-card)',
                      border: '1px solid var(--border-subtle)',
                      cursor: 'pointer',
                      transition: 'all var(--transition-fast)',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: '2px',
                      textAlign: 'left',
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.borderColor = 'var(--accent-primary)';
                      e.currentTarget.style.transform = 'translateY(-2px)';
                      e.currentTarget.style.boxShadow = '0 4px 14px rgba(59, 130, 246, 0.15)';
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.borderColor = 'var(--border-subtle)';
                      e.currentTarget.style.transform = 'translateY(0)';
                      e.currentTarget.style.boxShadow = 'none';
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '6px',
                        fontWeight: 600,
                        fontSize: '0.8125rem',
                        color: 'var(--text-primary)',
                      }}
                    >
                      <span style={{ fontSize: '1rem' }}>{item.icon}</span>
                      <span>{item.title}</span>
                    </div>
                    <div style={{ fontSize: '0.6875rem', color: 'var(--text-secondary)', lineHeight: '1.3' }}>
                      {item.description}
                    </div>
                  </div>
                ))}
              </div>

              <div
                style={{
                  fontSize: '0.6875rem',
                  color: 'var(--text-muted)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  background: 'var(--surface-glass-card)',
                  padding: '6px 12px',
                  borderRadius: 'var(--radius-md)',
                  border: '1px solid var(--border-subtle)',
                }}
              >
                <span>💡</span>
                <span>
                  <strong>Tip:</strong> Attach receipts or damage photos anytime for OCR verification.
                </span>
              </div>
            </div>
          ) : (
            messages.map((msg) => {
              const isCust = msg.sender_type === 'CUSTOMER';
              const isAI = msg.sender_type === 'AI_AGENT';
              const isHumanAgent = msg.sender_type === 'HUMAN_AGENT';

              let senderLabel = 'Customer';
              if (isAI) senderLabel = 'Customer Support Specialist';
              if (isHumanAgent) senderLabel = 'Lead Support Specialist';
              if (msg.sender_type === 'SYSTEM') senderLabel = 'System Notice';

              return (
                <div
                  key={msg.id}
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: isCust ? 'flex-end' : 'flex-start',
                  }}
                >
                  <div
                    style={{
                      fontSize: '0.6875rem',
                      color: 'var(--text-muted)',
                      marginBottom: '4px',
                      display: 'flex',
                      gap: '6px',
                    }}
                  >
                    <span>{senderLabel}</span>
                    <span>•</span>
                    <span>{msg.created_at}</span>
                    {msg.language && <span>• {msg.language.toUpperCase()}</span>}
                  </div>

                  <div
                    style={{
                      maxWidth: '78%',
                      padding: '14px 18px',
                      borderRadius: 'var(--radius-lg)',
                      background: isCust
                        ? 'var(--accent-primary)'
                        : isHumanAgent
                        ? 'var(--surface-glass-base)'
                        : 'var(--surface-glass-elevated)',
                      color: isCust ? '#ffffff' : 'var(--text-primary)',
                      border: isCust
                        ? 'none'
                        : isHumanAgent
                        ? '1px solid var(--accent-cyan)'
                        : 'var(--border-card)',
                      boxShadow: 'var(--shadow-sm)',
                      lineHeight: 1.5,
                      fontSize: '0.875rem',
                    }}
                  >
                    <SafeMarkdown content={msg.content} />

                    {/* Citations Block */}
                    {msg.citations && msg.citations.length > 0 && (
                      <div
                        style={{
                          marginTop: '12px',
                          paddingTop: '10px',
                          borderTop: isCust
                            ? '1px solid rgba(255, 255, 255, 0.2)'
                            : 'var(--border-subtle)',
                        }}
                      >
                        <div
                          style={{
                            fontSize: '0.6875rem',
                            fontWeight: 700,
                            color: isCust ? 'rgba(255,255,255,0.8)' : 'var(--text-muted)',
                            marginBottom: '6px',
                            letterSpacing: '0.04em',
                          }}
                        >
                          GROUNDED POLICY CITATIONS:
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
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
                              <div
                                style={{
                                  fontSize: '0.6875rem',
                                  color: 'var(--text-secondary)',
                                  fontStyle: 'italic',
                                }}
                              >
                                "{c.chunk_excerpt}"
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              );
            })
          )}

          {errorMsg && (
            <div
              style={{
                padding: '12px',
                borderRadius: 'var(--radius-md)',
                background: 'var(--accent-rose-light)',
                border: '1px solid rgba(244, 63, 94, 0.3)',
                color: 'var(--accent-rose)',
                fontSize: '0.8125rem',
              }}
            >
              <strong>API Notice:</strong> {errorMsg}
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Composer */}
        <div
          style={{
            padding: '14px 20px',
            borderTop: 'var(--border-subtle)',
            background: 'var(--surface-glass-base)',
          }}
        >
          <RichMarkdownChatInput
            onSubmit={handleSendMessage}
            loading={sendingMessage}
            onStop={handleStop}
            placeholder="Type your inquiry or message in Markdown... (Shift + Enter for new line)"
          />
        </div>
      </Card>

      {/* =========================================================================
          COLUMN 3: Ticket Synthesis & Clarification Drawer (Collapsible)
      ========================================================================= */}
      {isSynthesisOpen && (
        <Card
          variant="base"
          style={{
            display: 'flex',
            flexDirection: 'column',
            padding: '16px',
            overflowY: 'auto',
            background: 'var(--surface-glass-card)',
            borderLeft: 'var(--border-subtle)',
          }}
        >
          {/* Drawer Header */}
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              marginBottom: '16px',
            }}
          >
            <div>
              <div style={{ fontSize: '0.875rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                Ticket Synthesis
              </div>
              <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
                Structured evaluation & clarifications
              </div>
            </div>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setIsSynthesisOpen(false)}
              style={{ padding: '2px 6px' }}
            >
              ✕
            </Button>
          </div>

          {synthesisActionMsg && (
            <div
              style={{
                padding: '8px 10px',
                borderRadius: 'var(--radius-sm)',
                background: synthesisActionMsg.includes('error')
                  ? 'var(--accent-rose-light)'
                  : 'var(--accent-emerald-light)',
                color: synthesisActionMsg.includes('error')
                  ? 'var(--accent-rose)'
                  : 'var(--accent-emerald)',
                fontSize: '0.75rem',
                marginBottom: '12px',
              }}
            >
              {synthesisActionMsg}
            </div>
          )}

          {!linkedTicket ? (
            <div
              style={{
                padding: '16px 10px',
                textAlign: 'center',
                color: 'var(--text-muted)',
                fontSize: '0.75rem',
              }}
            >
              <p>No support ticket is currently linked to this conversation thread.</p>
              <p style={{ marginTop: '6px' }}>
                Tickets synthesized from conversation threads will display their completeness scores
                and clarification rounds here.
              </p>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              {/* Linked Ticket Meta */}
              <div
                style={{
                  padding: '10px',
                  borderRadius: 'var(--radius-md)',
                  background: 'var(--surface-glass-base)',
                  border: 'var(--border-subtle)',
                }}
              >
                <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                  Ticket #{linkedTicket.ticket_number}
                </div>
                <div
                  style={{
                    display: 'flex',
                    gap: '6px',
                    marginTop: '6px',
                    flexWrap: 'wrap',
                  }}
                >
                  <Badge variant="purple" size="sm">
                    {linkedTicket.priority}
                  </Badge>
                  <Badge variant="cyan" size="sm">
                    {linkedTicket.status}
                  </Badge>
                  <Badge variant="neutral" size="sm">
                    {synthesisData?.synthesis_status || linkedTicket.synthesis_status}
                  </Badge>
                </div>
              </div>

              {/* Completeness Bar */}
              {synthesisData && (
                <div>
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      fontSize: '0.75rem',
                      marginBottom: '4px',
                    }}
                  >
                    <span style={{ fontWeight: 600, color: 'var(--text-secondary)' }}>
                      Completeness Score
                    </span>
                    <span
                      style={{
                        fontWeight: 700,
                        color:
                          synthesisData.completeness_score >= 0.8
                            ? 'var(--accent-emerald)'
                            : synthesisData.completeness_score >= 0.5
                            ? 'var(--accent-amber)'
                            : 'var(--accent-rose)',
                      }}
                    >
                      {Math.round(synthesisData.completeness_score * 100)}%
                    </span>
                  </div>
                  <div
                    style={{
                      height: '6px',
                      borderRadius: 'var(--radius-full)',
                      background: 'rgba(255,255,255,0.1)',
                      overflow: 'hidden',
                    }}
                  >
                    <div
                      style={{
                        height: '100%',
                        width: `${Math.round(synthesisData.completeness_score * 100)}%`,
                        background:
                          synthesisData.completeness_score >= 0.8
                            ? 'var(--accent-emerald)'
                            : synthesisData.completeness_score >= 0.5
                            ? 'var(--accent-amber)'
                            : 'var(--accent-rose)',
                        transition: 'width 0.3s ease',
                      }}
                    />
                  </div>
                </div>
              )}

              {/* Problem Statement */}
              {synthesisData?.problem_statement && (
                <div>
                  <div
                    style={{
                      fontSize: '0.6875rem',
                      fontWeight: 700,
                      color: 'var(--text-muted)',
                      marginBottom: '4px',
                    }}
                  >
                    PROBLEM STATEMENT
                  </div>
                  <div
                    style={{
                      fontSize: '0.75rem',
                      color: 'var(--text-primary)',
                      lineHeight: 1.4,
                      background: 'var(--surface-glass-base)',
                      padding: '8px 10px',
                      borderRadius: 'var(--radius-sm)',
                    }}
                  >
                    {synthesisData.problem_statement}
                  </div>
                </div>
              )}

              {/* Reference Identifiers */}
              {synthesisData?.reference_identifiers &&
                synthesisData.reference_identifiers.length > 0 && (
                  <div>
                    <div
                      style={{
                        fontSize: '0.6875rem',
                        fontWeight: 700,
                        color: 'var(--text-muted)',
                        marginBottom: '4px',
                      }}
                    >
                      IDENTIFIERS
                    </div>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                      {synthesisData.reference_identifiers.map((ref, idx) => (
                        <span
                          key={idx}
                          style={{
                            fontSize: '0.6875rem',
                            fontFamily: 'var(--font-mono)',
                            padding: '2px 6px',
                            borderRadius: 'var(--radius-sm)',
                            background: 'var(--surface-glass-base)',
                            border: 'var(--border-subtle)',
                            color: 'var(--accent-cyan)',
                          }}
                        >
                          {ref.type || ref.key || 'REF'}: {ref.value || ref.identifier}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

              {/* Missing Fields */}
              {synthesisData?.missing_fields && synthesisData.missing_fields.length > 0 && (
                <div>
                  <div
                    style={{
                      fontSize: '0.6875rem',
                      fontWeight: 700,
                      color: 'var(--text-muted)',
                      marginBottom: '4px',
                    }}
                  >
                    MISSING MANDATORY FIELDS
                  </div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                    {synthesisData.missing_fields.map((f, idx) => (
                      <span
                        key={idx}
                        style={{
                          fontSize: '0.6875rem',
                          padding: '2px 6px',
                          borderRadius: 'var(--radius-sm)',
                          background: 'var(--accent-rose-light)',
                          color: 'var(--accent-rose)',
                          border: '1px solid rgba(244, 63, 94, 0.2)',
                        }}
                      >
                        {f}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Clarification Rounds */}
              <div>
                <div
                  style={{
                    fontSize: '0.6875rem',
                    fontWeight: 700,
                    color: 'var(--text-muted)',
                    marginBottom: '6px',
                  }}
                >
                  CLARIFICATION ROUNDS
                </div>
                {clarifications.length === 0 ? (
                  <div
                    style={{
                      fontSize: '0.75rem',
                      color: 'var(--text-muted)',
                      fontStyle: 'italic',
                    }}
                  >
                    No clarification rounds required or requested.
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {clarifications.map((round) => {
                      const isPending = round.status === 'PENDING_CUSTOMER_RESPONSE';
                      return (
                        <div
                          key={round.id}
                          style={{
                            padding: '8px 10px',
                            borderRadius: 'var(--radius-sm)',
                            background: 'var(--surface-glass-base)',
                            border: isPending
                              ? '1px solid var(--accent-amber)'
                              : 'var(--border-subtle)',
                          }}
                        >
                          <div
                            style={{
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'center',
                              marginBottom: '4px',
                            }}
                          >
                            <span style={{ fontSize: '0.6875rem', fontWeight: 600 }}>
                              Round #{round.round_number}
                            </span>
                            <Badge variant={isPending ? 'warning' : 'neutral'} size="sm">
                              {round.status}
                            </Badge>
                          </div>

                          {/* Questions */}
                          <div
                            style={{
                              fontSize: '0.75rem',
                              color: 'var(--text-secondary)',
                              marginBottom: '6px',
                            }}
                          >
                            {round.questions.map((q: any, i: number) => (
                              <div key={i} style={{ marginTop: '2px' }}>
                                • {typeof q === 'string' ? q : q.question_text || JSON.stringify(q)}
                              </div>
                            ))}
                          </div>

                          {/* Customer Response Input if Pending */}
                          {isPending && (
                            <div style={{ marginTop: '6px' }}>
                              <input
                                type="text"
                                placeholder="Type answer for clarification..."
                                value={clarificationInput}
                                onChange={(e) => setClarificationInput(e.target.value)}
                                style={{
                                  width: '100%',
                                  padding: '6px 8px',
                                  fontSize: '0.75rem',
                                  borderRadius: 'var(--radius-sm)',
                                  background: 'var(--surface-glass-input)',
                                  border: 'var(--border-card)',
                                  color: 'var(--text-primary)',
                                  outline: 'none',
                                  boxSizing: 'border-box',
                                  marginBottom: '6px',
                                }}
                              />
                              <Button
                                size="sm"
                                variant="primary"
                                loading={submittingClarification}
                                onClick={() => handleSubmitClarification(round.id)}
                                style={{ width: '100%', fontSize: '0.6875rem', padding: '4px 8px' }}
                              >
                                Submit Response
                              </Button>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>

              {/* Action Buttons */}
              <div
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '8px',
                  marginTop: '8px',
                  paddingTop: '10px',
                  borderTop: 'var(--border-subtle)',
                }}
              >
                <Button
                  size="sm"
                  variant="outline"
                  loading={isSynthesizing}
                  onClick={handleTriggerSynthesis}
                  style={{ width: '100%', fontSize: '0.75rem' }}
                >
                  ⚡ Synthesize Ticket
                </Button>

                {isStaff && (
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={isSynthesizing}
                    onClick={handleMarkComplete}
                    style={{ width: '100%', fontSize: '0.75rem' }}
                  >
                    ✓ Mark Synthesis Complete
                  </Button>
                )}
              </div>
            </div>
          )}
        </Card>
      )}
    </div>
  );
};
