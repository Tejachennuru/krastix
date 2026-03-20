import { useState, useEffect, useRef, useCallback } from 'react';
import { useAuth } from '../context/AuthContext';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

function UserAvatar({ name, email }) {
  const initials = name
    ? name.split(' ').map((n) => n[0]).join('').toUpperCase().slice(0, 2)
    : email?.[0]?.toUpperCase() ?? '?';
  return (
    <div className="w-8 h-8 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center text-xs font-bold text-white flex-shrink-0">
      {initials}
    </div>
  );
}

function TaskBadge({ count }) {
  return (
    <div className="flex items-center gap-2 bg-amber-500/10 border border-amber-500/30 text-amber-400 text-xs rounded-xl px-3 py-2">
      <span className="relative flex h-2 w-2">
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
        <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-400" />
      </span>
      {count} task{count !== 1 ? 's' : ''} running…
    </div>
  );
}

export default function ChatPage() {
  const { user, logout } = useAuth();
  const [domains, setDomains] = useState([]);
  const [selectedDomain, setSelectedDomain] = useState('');
  const [messages, setMessages] = useState([]);
  const [inputMessage, setInputMessage] = useState('');
  const [sessionId] = useState(() => crypto.randomUUID());
  const [loading, setLoading] = useState(false);
  const [pendingTasks, setPendingTasks] = useState([]);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const messagesEndRef = useRef(null);
  const textareaRef = useRef(null);

  useEffect(() => { fetchDomains(); }, []);

  const appendMessage = (role, content) => {
    setMessages((prev) => [...prev, { role, content, ts: Date.now() }]);
  };

  const checkPendingTasks = useCallback(async () => {
    const stillPending = [];
    for (const taskId of pendingTasks) {
      try {
        const res = await fetch(`${API_URL}/task/${taskId}?user_id=${user.user_id}`);
        const data = await res.json();
        if (data.status === 'completed') {
          appendMessage('assistant', `✅ Task completed:\n\`\`\`\n${JSON.stringify(data.result, null, 2)}\n\`\`\``);
        } else if (data.status === 'failed') {
          appendMessage('assistant', `❌ Task failed: ${data.error}`);
        } else {
          stillPending.push(taskId);
        }
      } catch {
        stillPending.push(taskId);
      }
    }
    setPendingTasks(stillPending);
  }, [pendingTasks, user.user_id]);

  useEffect(() => {
    if (pendingTasks.length > 0) {
      const interval = setInterval(checkPendingTasks, 3000);
      return () => clearInterval(interval);
    }
  }, [pendingTasks, checkPendingTasks]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const fetchDomains = async () => {
    try {
      const res = await fetch(`${API_URL}/domains`);
      const data = await res.json();
      if (Array.isArray(data) && data.length > 0) {
        setDomains(data);
        setSelectedDomain(data[0].domain_key);
      }
    } catch {
      // Backend may be starting up; domains remain empty
    }
  };

  const sendMessage = async () => {
    if (!inputMessage.trim() || !selectedDomain || loading) return;
    const text = inputMessage.trim();
    setInputMessage('');
    appendMessage('user', text);
    setLoading(true);
    if (textareaRef.current) textareaRef.current.style.height = 'auto';

    try {
      const res = await fetch(`${API_URL}/api/v1/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(user.token ? { Authorization: `Bearer ${user.token}` } : {}),
        },
        body: JSON.stringify({
          user_id: user.user_id,
          domain: selectedDomain,
          message: text,
          session_id: sessionId,
        }),
      });
      const data = await res.json();
      appendMessage('assistant', data.response || data.message || JSON.stringify(data));
      if (data.pending_tasks?.length > 0) {
        setPendingTasks((prev) => [...prev, ...data.pending_tasks]);
      }
    } catch (err) {
      appendMessage('assistant', `⚠️ Error: ${err.message}`);
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

  const autoResize = (e) => {
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px';
  };

  const startNewChat = () => {
    setMessages([]);
    setPendingTasks([]);
  };

  const domainLabel = domains.find((d) => d.domain_key === selectedDomain)?.display_name || selectedDomain;

  return (
    <div className="flex h-screen bg-[#0d0d14] text-white overflow-hidden">
      {/* ── Sidebar ── */}
      <aside
        className={`flex flex-col bg-[#13131f] border-r border-white/8 transition-all duration-300 ${
          sidebarOpen ? 'w-64' : 'w-0 overflow-hidden'
        }`}
      >
        {/* Logo */}
        <div className="flex items-center gap-3 px-5 py-5 border-b border-white/8">
          <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center shadow-md shadow-indigo-500/30 flex-shrink-0">
            <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
            </svg>
          </div>
          <span className="font-bold text-lg tracking-tight">Krastix</span>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
          {/* Domain Selector */}
          <div>
            <label className="text-xs font-medium text-slate-500 uppercase tracking-widest mb-2 block">
              Domain
            </label>
            <select
              value={selectedDomain}
              onChange={(e) => setSelectedDomain(e.target.value)}
              className="w-full bg-[#0d0d14] border border-white/10 text-white text-sm rounded-xl px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-indigo-500/50"
            >
              {domains.length === 0 ? (
                <option value="">Loading…</option>
              ) : (
                domains.map((d) => (
                  <option key={d.domain_key} value={d.domain_key}>
                    {d.display_name}
                  </option>
                ))
              )}
            </select>
          </div>

          {/* New Chat */}
          <button
            onClick={startNewChat}
            className="w-full flex items-center gap-2 bg-indigo-600/20 hover:bg-indigo-600/30 border border-indigo-500/30 text-indigo-300 text-sm font-medium rounded-xl px-3 py-2.5 transition-all"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
            New Conversation
          </button>

          {/* Pending Tasks */}
          {pendingTasks.length > 0 && <TaskBadge count={pendingTasks.length} />}

          {/* Placeholder recent chats */}
          <div>
            <label className="text-xs font-medium text-slate-500 uppercase tracking-widest mb-2 block">
              Recent
            </label>
            {messages.length > 0 ? (
              <div className="bg-white/5 hover:bg-white/8 rounded-xl px-3 py-2.5 text-sm text-slate-300 cursor-pointer truncate border border-white/5 transition-all">
                {messages[0]?.content?.slice(0, 40) || 'Current chat'}…
              </div>
            ) : (
              <p className="text-xs text-slate-600 px-1">No recent conversations</p>
            )}
          </div>
        </div>

        {/* User Footer */}
        <div className="border-t border-white/8 px-4 py-4">
          <div className="flex items-center gap-3 mb-3">
            <UserAvatar name={user.full_name} email={user.email} />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-white truncate">{user.full_name || 'User'}</p>
              <p className="text-xs text-slate-500 truncate">{user.email}</p>
            </div>
          </div>
          <button
            onClick={logout}
            className="w-full flex items-center gap-2 text-slate-400 hover:text-red-400 text-sm px-2 py-1.5 rounded-lg hover:bg-red-500/10 transition-all"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 9V5.25A2.25 2.25 0 0013.5 3h-6a2.25 2.25 0 00-2.25 2.25v13.5A2.25 2.25 0 007.5 21h6a2.25 2.25 0 002.25-2.25V15M12 9l-3 3m0 0l3 3m-3-3h12.75" />
            </svg>
            Sign Out
          </button>
        </div>
      </aside>

      {/* ── Main Area ── */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <header className="flex items-center gap-3 px-5 py-4 border-b border-white/8 bg-[#13131f]/50 backdrop-blur">
          <button
            onClick={() => setSidebarOpen((o) => !o)}
            className="p-1.5 text-slate-400 hover:text-white rounded-lg hover:bg-white/5 transition-all"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5M3.75 17.25h16.5" />
            </svg>
          </button>
          <div className="flex-1">
            <h2 className="text-sm font-semibold text-white">
              {domainLabel || 'AI Assistant'}
            </h2>
            <p className="text-xs text-slate-500">Krastix Orchestrator</p>
          </div>
          {pendingTasks.length > 0 && (
            <div className="text-xs text-amber-400 flex items-center gap-1.5">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-400" />
              </span>
              {pendingTasks.length} task{pendingTasks.length !== 1 ? 's' : ''} running
            </div>
          )}
        </header>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-6">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-center max-w-md mx-auto">
              <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center shadow-xl shadow-indigo-500/30 mb-6">
                <svg className="w-8 h-8 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
                </svg>
              </div>
              <h2 className="text-2xl font-bold text-white mb-2">
                Welcome{user.full_name ? `, ${user.full_name.split(' ')[0]}` : ''}!
              </h2>
              <p className="text-slate-400 text-sm leading-relaxed mb-6">
                I'm your AI orchestrator. Ask me to research candidates, manage CRM data, create forms, or anything in between.
              </p>
              <div className="grid grid-cols-1 gap-2 w-full">
                {[
                  'Find LinkedIn profiles for senior React developers',
                  'Create a job application form for a backend engineer role',
                  'Show me the latest candidates in the pipeline',
                ].map((suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => setInputMessage(suggestion)}
                    className="text-left text-sm bg-white/5 hover:bg-white/8 border border-white/8 rounded-xl px-4 py-3 text-slate-300 hover:text-white transition-all"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="max-w-3xl mx-auto space-y-6">
              {messages.map((msg, idx) => (
                <div key={idx} className={`flex gap-3 ${msg.role === 'user' ? 'flex-row-reverse' : 'flex-row'}`}>
                  {msg.role === 'user' ? (
                    <UserAvatar name={user.full_name} email={user.email} />
                  ) : (
                    <div className="w-8 h-8 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center flex-shrink-0">
                      <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
                      </svg>
                    </div>
                  )}
                  <div className={`max-w-[75%] ${msg.role === 'user' ? 'items-end' : 'items-start'} flex flex-col gap-1`}>
                    <span className="text-xs text-slate-500 px-1">
                      {msg.role === 'user' ? (user.full_name?.split(' ')[0] || 'You') : 'Krastix'}
                    </span>
                    <div
                      className={`rounded-2xl px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap break-words ${
                        msg.role === 'user'
                          ? 'bg-indigo-600 text-white rounded-tr-sm'
                          : 'bg-[#1e1e2e] border border-white/8 text-slate-200 rounded-tl-sm'
                      }`}
                    >
                      {msg.content}
                    </div>
                  </div>
                </div>
              ))}

              {loading && (
                <div className="flex gap-3">
                  <div className="w-8 h-8 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center flex-shrink-0">
                    <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
                    </svg>
                  </div>
                  <div className="bg-[#1e1e2e] border border-white/8 rounded-2xl rounded-tl-sm px-4 py-3">
                    <div className="flex gap-1.5 items-center h-4">
                      <span className="w-2 h-2 bg-indigo-400 rounded-full animate-bounce [animation-delay:0ms]" />
                      <span className="w-2 h-2 bg-indigo-400 rounded-full animate-bounce [animation-delay:150ms]" />
                      <span className="w-2 h-2 bg-indigo-400 rounded-full animate-bounce [animation-delay:300ms]" />
                    </div>
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        {/* Input Area */}
        <div className="px-4 pb-5 pt-3 border-t border-white/8 bg-[#13131f]/50 backdrop-blur">
          <div className="max-w-3xl mx-auto">
            <div className="flex items-end gap-3 bg-[#1e1e2e] border border-white/10 rounded-2xl px-4 py-3 focus-within:ring-2 focus-within:ring-indigo-500/40 focus-within:border-indigo-500/40 transition-all">
              <textarea
                ref={textareaRef}
                value={inputMessage}
                onChange={(e) => { setInputMessage(e.target.value); autoResize(e); }}
                onKeyDown={handleKeyDown}
                placeholder={`Message ${domainLabel || 'AI Assistant'}…`}
                rows={1}
                className="flex-1 bg-transparent text-white text-sm placeholder-slate-600 resize-none focus:outline-none min-h-[24px] max-h-40"
              />
              <button
                onClick={sendMessage}
                disabled={loading || !inputMessage.trim()}
                className="w-9 h-9 flex items-center justify-center bg-indigo-600 hover:bg-indigo-500 disabled:bg-slate-700 disabled:cursor-not-allowed rounded-xl transition-all flex-shrink-0 shadow-md shadow-indigo-500/20"
              >
                <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
                </svg>
              </button>
            </div>
            <p className="text-center text-xs text-slate-600 mt-2">
              Press <kbd className="font-mono bg-white/5 px-1.5 py-0.5 rounded text-slate-500">Enter</kbd> to send,{' '}
              <kbd className="font-mono bg-white/5 px-1.5 py-0.5 rounded text-slate-500">Shift+Enter</kbd> for a new line.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
