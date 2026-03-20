import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

const DOMAINS = [
  { key: 'HR_RECRUITER', label: 'HR Recruitment', icon: '👥', description: 'Candidate sourcing, screening & coordination' },
  { key: 'PERSONAL_ASSISTANT', label: 'Personal Assistant', icon: '🧠', description: 'Scheduling, tasks & life management' },
];

function generateSessionId() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function formatTime(date) {
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export default function ChatPage() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const [selectedDomain, setSelectedDomain] = useState(DOMAINS[0].key);
  const [sessions, setSessions] = useState(() => {
    const id = generateSessionId();
    return [{ id, label: 'New conversation', domain: DOMAINS[0].key, messages: [], createdAt: Date.now() }];
  });
  const [activeSessionId, setActiveSessionId] = useState(() => sessions[0]?.id);
  const [inputMessage, setInputMessage] = useState('');
  const [loading, setLoading] = useState(false);
  const [pendingTasks, setPendingTasks] = useState([]);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const messagesEndRef = useRef(null);

  const activeSession = sessions.find((s) => s.id === activeSessionId);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [activeSession?.messages, loading]);

  // Poll pending tasks
  useEffect(() => {
    if (pendingTasks.length === 0) return;
    const interval = setInterval(async () => {
      const remaining = [];
      for (const task of pendingTasks) {
        try {
          const res = await fetch(`${API_URL}/api/v1/tasks/${task.id}?user_id=${user.id}`);
          if (!res.ok) { remaining.push(task); continue; }
          const data = await res.json();
          if (data.status === 'completed' || data.status === 'success') {
            addMessage(task.sessionId, {
              role: 'system',
              content: `✅ Task completed: ${JSON.stringify(data.result, null, 2)}`,
              time: new Date(),
            });
          } else if (data.status === 'failed') {
            addMessage(task.sessionId, {
              role: 'system',
              content: `❌ Task failed: ${data.error}`,
              time: new Date(),
            });
          } else {
            remaining.push(task);
          }
        } catch {
          remaining.push(task);
        }
      }
      setPendingTasks(remaining);
    }, 3000);
    return () => clearInterval(interval);
  }, [pendingTasks, user.id, addMessage]);

  const addMessage = useCallback((sessionId, message) => {
    setSessions((prev) =>
      prev.map((s) => {
        if (s.id !== sessionId) return s;
        const updatedMessages = [...s.messages, message];
        const label = s.messages.length === 0
          ? (message.content?.slice(0, 35) || 'Conversation')
          : s.label;
        return { ...s, messages: updatedMessages, label };
      })
    );
  }, []);

  const sendMessage = async () => {
    if (!inputMessage.trim() || loading || !activeSession) return;

    const userMsg = { role: 'user', content: inputMessage.trim(), time: new Date() };
    addMessage(activeSessionId, userMsg);
    const currentInput = inputMessage.trim();
    setInputMessage('');
    setLoading(true);

    try {
      const res = await fetch(`${API_URL}/api/v1/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: user.id,
          domain: selectedDomain,
          message: currentInput,
          session_id: activeSessionId,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.message || err.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();

      addMessage(activeSessionId, {
        role: 'assistant',
        content: data.response || data.message || JSON.stringify(data),
        time: new Date(),
      });

      if (data.pending_tasks?.length > 0) {
        const newTasks = data.pending_tasks.map((id) => ({ id, sessionId: activeSessionId }));
        setPendingTasks((prev) => [...prev, ...newTasks]);
      }
    } catch (err) {
      addMessage(activeSessionId, {
        role: 'error',
        content: `Error: ${err.message}`,
        time: new Date(),
      });
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const newConversation = () => {
    const id = generateSessionId();
    const session = {
      id,
      label: 'New conversation',
      domain: selectedDomain,
      messages: [],
      createdAt: Date.now(),
    };
    setSessions((prev) => [session, ...prev]);
    setActiveSessionId(id);
  };

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  const activeDomain = DOMAINS.find((d) => d.key === selectedDomain);

  return (
    <div className="min-h-screen bg-[#0d0d1a] flex overflow-hidden" style={{ height: '100vh' }}>
      {/* Sidebar */}
      <aside
        className={`${
          sidebarOpen ? 'w-72' : 'w-0 overflow-hidden'
        } flex-shrink-0 bg-[#13131f] border-r border-white/5 flex flex-col transition-all duration-300`}
      >
        {/* Sidebar Header */}
        <div className="p-4 border-b border-white/5">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-violet-600 to-blue-600 flex items-center justify-center shadow-lg shadow-violet-500/20 flex-shrink-0">
              <svg className="w-5 h-5 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.091z" />
                <path strokeLinecap="round" strokeLinejoin="round" d="M18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z" />
              </svg>
            </div>
            <div className="min-w-0">
              <h1 className="text-white font-bold text-base leading-tight">Krastix</h1>
              <p className="text-gray-500 text-xs truncate">AI Orchestration</p>
            </div>
          </div>
        </div>

        {/* Domain Selector */}
        <div className="p-4 border-b border-white/5">
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Domain</p>
          <div className="space-y-1">
            {DOMAINS.map((d) => (
              <button
                key={d.key}
                onClick={() => setSelectedDomain(d.key)}
                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-left transition ${
                  selectedDomain === d.key
                    ? 'bg-violet-600/20 border border-violet-500/30 text-violet-300'
                    : 'text-gray-400 hover:bg-white/5 hover:text-gray-200 border border-transparent'
                }`}
              >
                <span className="text-lg leading-none">{d.icon}</span>
                <div className="min-w-0">
                  <div className="text-sm font-medium truncate">{d.label}</div>
                  <div className="text-xs text-gray-500 truncate">{d.description}</div>
                </div>
              </button>
            ))}
          </div>
        </div>

        {/* New Chat Button */}
        <div className="p-4">
          <button
            onClick={newConversation}
            className="w-full flex items-center justify-center gap-2 bg-gradient-to-r from-violet-600/80 to-blue-600/80 hover:from-violet-600 hover:to-blue-600 text-white rounded-xl py-2.5 px-4 text-sm font-semibold transition shadow-lg shadow-violet-500/10"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
            New conversation
          </button>
        </div>

        {/* Sessions List */}
        <div className="flex-1 overflow-y-auto px-3 pb-2">
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider px-1 mb-2">Recent</p>
          <div className="space-y-0.5">
            {sessions.map((s) => (
              <button
                key={s.id}
                onClick={() => setActiveSessionId(s.id)}
                className={`w-full flex items-start gap-2 px-3 py-2.5 rounded-xl text-left transition group ${
                  activeSessionId === s.id
                    ? 'bg-white/10 text-white'
                    : 'text-gray-400 hover:bg-white/5 hover:text-gray-200'
                }`}
              >
                <svg className="w-4 h-4 mt-0.5 flex-shrink-0 opacity-60" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M8.625 12a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 01-2.555-.337A5.972 5.972 0 015.41 20.97a5.969 5.969 0 01-.474-.065 4.48 4.48 0 00.978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25z" />
                </svg>
                <span className="text-sm truncate">{s.label}</span>
              </button>
            ))}
          </div>
        </div>

        {/* Pending tasks indicator */}
        {pendingTasks.length > 0 && (
          <div className="mx-4 mb-3 px-3 py-2.5 rounded-xl bg-amber-500/10 border border-amber-500/30 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse flex-shrink-0" />
            <span className="text-xs text-amber-300 truncate">{pendingTasks.length} task{pendingTasks.length > 1 ? 's' : ''} running…</span>
          </div>
        )}

        {/* User info / logout */}
        <div className="p-4 border-t border-white/5">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-full bg-gradient-to-br from-violet-500 to-blue-500 flex items-center justify-center flex-shrink-0 text-xs font-bold text-white">
              {user.name?.charAt(0).toUpperCase() || user.email?.charAt(0).toUpperCase()}
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium text-white truncate">{user.name || 'User'}</div>
              <div className="text-xs text-gray-500 truncate">{user.email}</div>
            </div>
            <button
              onClick={handleLogout}
              title="Sign out"
              className="flex-shrink-0 text-gray-500 hover:text-red-400 transition p-1 rounded-lg hover:bg-white/5"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 9V5.25A2.25 2.25 0 0013.5 3h-6a2.25 2.25 0 00-2.25 2.25v13.5A2.25 2.25 0 007.5 21h6a2.25 2.25 0 002.25-2.25V15M12 9l-3 3m0 0l3 3m-3-3h12.75" />
              </svg>
            </button>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 flex flex-col min-w-0">
        {/* Top Bar */}
        <header className="flex-shrink-0 flex items-center gap-4 px-4 py-3 border-b border-white/5 bg-[#13131f]/60 backdrop-blur">
          <button
            onClick={() => setSidebarOpen((v) => !v)}
            className="text-gray-400 hover:text-white transition p-1.5 rounded-lg hover:bg-white/5"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8">
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
            </svg>
          </button>
          <div className="flex items-center gap-2">
            <span className="text-lg">{activeDomain?.icon}</span>
            <span className="text-white font-semibold text-sm">{activeDomain?.label}</span>
          </div>
          <div className="flex-1" />
          <div className="hidden sm:flex items-center gap-1.5 bg-[#1e1e2e] border border-white/10 rounded-full px-3 py-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
            <span className="text-xs text-gray-400">AI Ready</span>
          </div>
        </header>

        {/* Messages Area */}
        <div className="flex-1 overflow-y-auto px-4 py-6">
          {!activeSession || activeSession.messages.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-center px-4">
              <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-violet-600 to-blue-600 flex items-center justify-center shadow-2xl shadow-violet-500/30 mb-5">
                <svg className="w-9 h-9 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.091z" />
                  <path strokeLinecap="round" strokeLinejoin="round" d="M18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z" />
                </svg>
              </div>
              <h2 className="text-2xl font-bold text-white mb-2">
                Welcome, {user.name?.split(' ')[0] || 'there'}!
              </h2>
              <p className="text-gray-400 max-w-md text-sm mb-8">
                You're connected to the <span className="text-violet-400 font-medium">{activeDomain?.label}</span>. Ask me anything — I'll coordinate the right agents to get it done.
              </p>

              {/* Suggestion chips */}
              <div className="flex flex-wrap gap-2 justify-center max-w-lg">
                {selectedDomain === 'HR_RECRUITER' && [
                  'Find React developers on LinkedIn',
                  'Screen resumes for a senior engineer role',
                  'Schedule 5 candidate interviews for next week',
                  'Create an application form for a product designer',
                ].map((s) => (
                  <button
                    key={s}
                    onClick={() => setInputMessage(s)}
                    className="text-xs bg-white/5 hover:bg-white/10 border border-white/10 hover:border-violet-500/40 text-gray-300 hover:text-white px-3 py-2 rounded-xl transition"
                  >
                    {s}
                  </button>
                ))}
                {selectedDomain === 'PERSONAL_ASSISTANT' && [
                  'Plan my week schedule',
                  'Book a restaurant for Saturday evening',
                  'Summarize my pending tasks',
                  'Research the latest AI news',
                ].map((s) => (
                  <button
                    key={s}
                    onClick={() => setInputMessage(s)}
                    className="text-xs bg-white/5 hover:bg-white/10 border border-white/10 hover:border-violet-500/40 text-gray-300 hover:text-white px-3 py-2 rounded-xl transition"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="max-w-3xl mx-auto space-y-4">
              {activeSession.messages.map((msg, idx) => (
                <MessageBubble key={idx} message={msg} userName={user.name} />
              ))}
              {loading && <TypingIndicator />}
              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        {/* Input Area */}
        <div className="flex-shrink-0 border-t border-white/5 bg-[#13131f]/60 backdrop-blur px-4 py-4">
          <div className="max-w-3xl mx-auto">
            <div className="flex gap-3 items-end bg-[#1e1e2e] border border-white/10 rounded-2xl px-4 py-3 focus-within:border-violet-500/40 focus-within:ring-1 focus-within:ring-violet-500/20 transition">
              <textarea
                value={inputMessage}
                onChange={(e) => setInputMessage(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={`Message ${activeDomain?.label}…`}
                rows={1}
                className="flex-1 bg-transparent text-white placeholder-gray-500 text-sm resize-none focus:outline-none leading-relaxed"
                style={{ minHeight: '1.5rem', maxHeight: '8rem', overflowY: 'auto' }}
                onInput={(e) => {
                  e.target.style.height = 'auto';
                  e.target.style.height = Math.min(e.target.scrollHeight, 128) + 'px';
                }}
              />
              <button
                onClick={sendMessage}
                disabled={loading || !inputMessage.trim()}
                className="flex-shrink-0 w-9 h-9 rounded-xl bg-gradient-to-br from-violet-600 to-blue-600 hover:from-violet-500 hover:to-blue-500 disabled:from-gray-700 disabled:to-gray-700 disabled:cursor-not-allowed flex items-center justify-center transition shadow-lg shadow-violet-500/20"
              >
                {loading ? (
                  <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                ) : (
                  <svg className="w-4 h-4 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
                  </svg>
                )}
              </button>
            </div>
            <p className="text-center text-xs text-gray-600 mt-2">
              Press <kbd className="bg-white/5 px-1.5 py-0.5 rounded text-gray-500 font-mono text-xs">Enter</kbd> to send · <kbd className="bg-white/5 px-1.5 py-0.5 rounded text-gray-500 font-mono text-xs">Shift+Enter</kbd> for new line
            </p>
          </div>
        </div>
      </main>
    </div>
  );
}

function MessageBubble({ message, userName }) {
  const isUser = message.role === 'user';
  const isError = message.role === 'error';
  const isSystem = message.role === 'system';

  if (isSystem) {
    return (
      <div className="flex justify-center">
        <div className="text-xs bg-white/5 border border-white/10 rounded-full px-4 py-1.5 text-gray-400 max-w-lg text-center">
          {message.content}
        </div>
      </div>
    );
  }

  return (
    <div className={`flex gap-3 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}>
      {/* Avatar */}
      <div className={`flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold ${
        isUser
          ? 'bg-gradient-to-br from-violet-500 to-blue-500 text-white'
          : isError
          ? 'bg-red-500/20 text-red-400 border border-red-500/30'
          : 'bg-gradient-to-br from-violet-600/40 to-blue-600/40 border border-violet-500/20 text-violet-300'
      }`}>
        {isUser ? (userName?.charAt(0).toUpperCase() || 'U') : isError ? '!' : '✦'}
      </div>

      {/* Bubble */}
      <div className={`max-w-[75%] ${isUser ? 'items-end' : 'items-start'} flex flex-col gap-1`}>
        <div className={`px-4 py-3 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap ${
          isUser
            ? 'bg-gradient-to-br from-violet-600 to-blue-600 text-white rounded-tr-sm'
            : isError
            ? 'bg-red-500/10 border border-red-500/30 text-red-300 rounded-tl-sm'
            : 'bg-[#1e1e2e] border border-white/10 text-gray-200 rounded-tl-sm'
        }`}>
          {message.content}
        </div>
        {message.time && (
          <span className="text-xs text-gray-600 px-1">{formatTime(message.time)}</span>
        )}
      </div>
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="flex gap-3 items-start">
      <div className="flex-shrink-0 w-8 h-8 rounded-full bg-gradient-to-br from-violet-600/40 to-blue-600/40 border border-violet-500/20 flex items-center justify-center text-violet-300 text-xs font-bold">
        ✦
      </div>
      <div className="bg-[#1e1e2e] border border-white/10 rounded-2xl rounded-tl-sm px-4 py-3 flex items-center gap-1.5">
        <span className="w-2 h-2 bg-violet-400 rounded-full animate-bounce [animation-delay:-0.3s]" />
        <span className="w-2 h-2 bg-violet-400 rounded-full animate-bounce [animation-delay:-0.15s]" />
        <span className="w-2 h-2 bg-violet-400 rounded-full animate-bounce" />
      </div>
    </div>
  );
}
