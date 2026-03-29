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
  const [selectedDomain, setSelectedDomain] = useState(() => {
    return localStorage.getItem('krastix_selected_domain') || '';
  });
  const [messages, setMessages] = useState([]);
  const [inputMessage, setInputMessage] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [recentChats, setRecentChats] = useState([]);

  // Load/Generate session ID when domain changes
  useEffect(() => {
    if (selectedDomain) {
      const storageKey = `krastix_session_id_${selectedDomain}`;
      let saved = localStorage.getItem(storageKey);
      if (!saved) {
        saved = crypto.randomUUID();
        localStorage.setItem(storageKey, saved);
      }
      setSessionId(saved);
      localStorage.setItem('krastix_session_id', saved); // Backward compatibility/Main active session
    }
  }, [selectedDomain]);

  // Restored state hooks
  const [loading, setLoading] = useState(false);
  const [pendingTasks, setPendingTasks] = useState([]);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const messagesEndRef = useRef(null);
  const textareaRef = useRef(null);
  const handledTaskIdsRef = useRef(new Set());

  const [showIntegrations, setShowIntegrations] = useState(false);
  const [integrationTokens, setIntegrationTokens] = useState({ tally: '', jotform: '' });
  const [activeIntegrations, setActiveIntegrations] = useState([]);
  const [tallyForms, setTallyForms] = useState([]);
  const [loadingTallyForms, setLoadingTallyForms] = useState(false);
  const [storedApplicants, setStoredApplicants] = useState([]);
  const [loadingStoredApplicants, setLoadingStoredApplicants] = useState(false);
  const [applicantFormFilter, setApplicantFormFilter] = useState('');

  useEffect(() => { fetchDomains(); }, []);

  const fetchIntegrations = async () => {
    try {
      const res = await fetch(`${API_URL}/api/v1/integrations/${user.user_id}`);
      const data = await res.json();
      setActiveIntegrations(data.map(i => i.provider));
    } catch (err) {
      console.error('Failed to load integrations', err);
    }
  };

  const fetchTallyForms = async () => {
    if (!user?.user_id) return;
    setLoadingTallyForms(true);
    try {
      const res = await fetch(`${API_URL}/api/v1/forms/tally/${user.user_id}`);
      const data = await res.json();
      if (data?.status === 'success' && Array.isArray(data.forms)) {
        setTallyForms(data.forms);
      } else {
        setTallyForms([]);
      }
    } catch (err) {
      console.error('Failed to load tally forms', err);
      setTallyForms([]);
    } finally {
      setLoadingTallyForms(false);
    }
  };

  const fetchStoredApplicants = async (formFilter = '') => {
    if (!user?.user_id) return;
    setLoadingStoredApplicants(true);
    try {
      const suffix = formFilter ? `&form_id=${encodeURIComponent(formFilter)}` : '';
      const res = await fetch(`${API_URL}/api/v1/applicants/stored?user_id=${user.user_id}${suffix}`);
      const data = await res.json();
      if (data?.status === 'success' && Array.isArray(data.items)) {
        setStoredApplicants(data.items);
      } else {
        setStoredApplicants([]);
      }
    } catch (err) {
      console.error('Failed to load stored applicants', err);
      setStoredApplicants([]);
    } finally {
      setLoadingStoredApplicants(false);
    }
  };

  useEffect(() => {
    if (showIntegrations) {
      fetchIntegrations();
      if (activeIntegrations.includes('tally')) {
        fetchTallyForms();
      }
    }
  }, [showIntegrations]);

  useEffect(() => {
    if (showIntegrations && activeIntegrations.includes('tally')) {
      fetchTallyForms();
      fetchStoredApplicants(applicantFormFilter);
    }
  }, [showIntegrations, activeIntegrations.length]);

  useEffect(() => {
    if (showIntegrations && activeIntegrations.includes('tally')) {
      fetchStoredApplicants(applicantFormFilter);
    }
  }, [applicantFormFilter]);

  const saveIntegration = async (provider) => {
    const token = integrationTokens[provider];
    if (!token) return;
    try {
      const res = await fetch(`${API_URL}/api/v1/integrations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: user.user_id, provider, access_token: token })
      });
      if (res.ok) {
        alert(`${provider} connected successfully!`);
        fetchIntegrations();
        setIntegrationTokens(prev => ({ ...prev, [provider]: '' }));
      }
    } catch (err) {
      alert(`Failed to connect ${provider}`);
    }
  };

  const connectGoogle = async () => {
    try {
      const res = await fetch(`${API_URL}/api/v1/integrations/google/oauth/start?user_id=${encodeURIComponent(user.user_id)}`);
      const data = await res.json();
      if (!res.ok || !data.auth_url) {
        const detail = data?.detail;
        if (detail?.message && Array.isArray(detail?.missing) && detail.missing.length > 0) {
          alert(`${detail.message}. Missing: ${detail.missing.join(', ')}`);
        } else if (typeof detail === 'string' && detail.trim()) {
          alert(`Unable to start Google sign-in: ${detail}`);
        } else {
          alert('Unable to start Google sign-in. Check backend OAuth config.');
        }
        return;
      }

      const popup = window.open(
        data.auth_url,
        'google-oauth',
        'width=520,height=700,menubar=no,toolbar=no,status=no'
      );

      if (!popup) {
        alert('Popup blocked. Please allow popups and try again.');
      }
    } catch (err) {
      console.error('Failed to start Google OAuth', err);
      alert('Failed to start Google OAuth flow.');
    }
  };

  useEffect(() => {
    const onOauthMessage = (event) => {
      const payload = event?.data;
      if (!payload || payload.type !== 'google-oauth-complete') return;

      if (payload.success) {
        fetchIntegrations();
      } else {
        alert(payload.error || 'Google sign-in failed.');
      }
    };

    window.addEventListener('message', onOauthMessage);
    return () => window.removeEventListener('message', onOauthMessage);
  }, [user?.user_id]);

  // Fetch Conversation History
  useEffect(() => {
    if (user?.user_id && sessionId) {
      console.log("Fetching history for session:", sessionId);
      fetch(`${API_URL}/api/v1/chat/history?session_id=${sessionId}&user_id=${user.user_id}`)
        .then(res => res.json())
        .then(data => {
          if (Array.isArray(data)) {
            setMessages(data.map(m => ({ 
              role: m.role, 
              content: m.content, 
              ts: m.timestamp ? new Date(m.timestamp.replace(' ', 'T')).getTime() : Date.now() 
            })));
          }
        }).catch(err => console.error("Error fetching history:", err));
    }
  }, [user, sessionId]);

  // Fetch Recent Chats for Sidebar
  const fetchRecentChats = async () => {
    if (!user?.user_id) return;
    try {
      const res = await fetch(`${API_URL}/api/v1/conversations?user_id=${user.user_id}${selectedDomain ? `&domain_key=${selectedDomain}` : ''}`);
      const data = await res.json();
      setRecentChats(data);
    } catch (err) {
      console.error("Error fetching recent chats:", err);
    }
  };

  useEffect(() => {
    fetchRecentChats();
  }, [user, selectedDomain, messages.length]); // Refresh list when messages are added

  const appendMessage = (role, content) => {
    setMessages((prev) => [...prev, { role, content, ts: Date.now() }]);
  };

  const checkPendingTasks = useCallback(async () => {
    const stillPending = [];
    for (const taskId of pendingTasks) {
      if (handledTaskIdsRef.current.has(taskId)) {
        continue;
      }
      try {
        const res = await fetch(`${API_URL}/task/${taskId}?user_id=${user.user_id}`);
        if (res.status === 404) {
          stillPending.push(taskId);
          continue;
        }

        const data = await res.json();

        if (data.status === 'completed' || data.status === 'success') {
          const result = data.result || {};
          const innerData = result.data || {};
          if (result.action === 'create_form' && innerData.form_url) {
            appendMessage(
              'assistant',
              `Task complete. Form "${innerData.form_title || 'Generated Form'}" is ready.\nPublic link: ${innerData.form_url}${innerData.edit_url ? `\nEdit link: ${innerData.edit_url}` : ''}`
            );
          } else if (result.action === 'list_form_responses') {
            const count = innerData.applicants_count ?? (Array.isArray(innerData.applicants) ? innerData.applicants.length : 0);
            appendMessage(
              'assistant',
              `Task complete. Retrieved ${count} response${count === 1 ? '' : 's'} for form ${innerData.form_url || innerData.form_id || ''}.`
            );
          } else {
            appendMessage('assistant', `Task complete.\n${JSON.stringify(data.result, null, 2)}`);
          }
          handledTaskIdsRef.current.add(taskId);
        } else if (data.status === 'failed' || data.status === 'stale') {
          appendMessage('assistant', `Task failed: ${data.error || 'Timeout or unknown error'}`);
          handledTaskIdsRef.current.add(taskId);
        } else {
          stillPending.push(taskId);
        }
      } catch (err) {
        console.error("Task Polling Error:", err);
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
        const saved = localStorage.getItem('krastix_selected_domain');
        const exists = data.some(d => d.domain_key === saved);
        if (exists) {
          setSelectedDomain(saved);
        } else {
          setSelectedDomain(data[0].domain_key);
          localStorage.setItem('krastix_selected_domain', data[0].domain_key);
        }
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
      const res = await fetch(`${API_URL}/api/v1/chat/stream`, {
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

      if (!res.ok) {
        throw new Error(`Server returned ${res.status}`);
      }

      setLoading(false);
      let streamedResponse = '';

      // Append a placeholder message that we will actively update
      appendMessage('assistant', '...');

      const reader = res.body.getReader();
      const decoder = new TextDecoder();

      // --- Robust SSE parser using a line buffer ---
      // SSE events are separated by blank lines (\n\n).
      // Chunks from the reader may split an event across reads.
      let sseBuffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        sseBuffer += decoder.decode(value, { stream: true });

        // Process all complete events (separated by double newlines)
        const parts = sseBuffer.split('\n\n');
        // The last part may be incomplete — keep it in the buffer
        sseBuffer = parts.pop() || '';

        for (const event of parts) {
          if (!event.trim()) continue;

          const eventMatch = event.match(/event: (.*)/);
          const dataMatch = event.match(/data: (.*)/s);

          if (!dataMatch) continue;

          const eventType = eventMatch ? eventMatch[1].trim() : 'token';
          let parsedData = dataMatch[1].trim();
          try { parsedData = JSON.parse(parsedData); } catch (e) { }

          if (eventType === 'token') {
            streamedResponse += parsedData;
            setMessages(prev => {
              const newMessages = [...prev];
              if (newMessages[newMessages.length - 1].role === 'assistant') {
                newMessages[newMessages.length - 1].content = streamedResponse || '...';
              }
              return newMessages;
            });
          } else if (eventType === 'tool_start') {
            const toolMsg = `\n\nAgent is executing a delegated task (${parsedData.tool}). You will be notified when results are ready.`;
            streamedResponse += toolMsg;
            setMessages(prev => {
              const newMessages = [...prev];
              if (newMessages[newMessages.length - 1].role === 'assistant') {
                newMessages[newMessages.length - 1].content = streamedResponse;
              }
              return newMessages;
            });
          } else if (eventType === 'tool_result') {
            // Fallback: extract task_id from the ToolMessage content
            // Format: "Task <uuid> dispatched to <queue>. ..."
            const resultStr = typeof parsedData === 'string' ? parsedData : '';
            const taskMatch = resultStr.match(/Task ([0-9a-f-]{36}) dispatched/i);
            if (taskMatch) {
              const extractedId = taskMatch[1];
              console.log('[SSE] task_id extracted from tool_result:', extractedId);
              setPendingTasks(prev => {
                if (prev.includes(extractedId)) return prev;
                return [...prev, extractedId];
              });
            }
          } else if (eventType === 'done') {
            if (parsedData && typeof parsedData === 'object' && typeof parsedData.response === 'string') {
              streamedResponse = parsedData.response;
              setMessages(prev => {
                const newMessages = [...prev];
                if (newMessages[newMessages.length - 1].role === 'assistant') {
                  newMessages[newMessages.length - 1].content = streamedResponse || '...';
                }
                return newMessages;
              });
            }

            if (parsedData && parsedData.task_id) {
              console.log('[SSE] task_id from done event:', parsedData.task_id);
              setPendingTasks(prev => {
                if (prev.includes(parsedData.task_id)) return prev;
                return [...prev, parsedData.task_id];
              });
            }
          }
        }
      }
    } catch (err) {
      appendMessage('assistant', `⚠️ Network Error: LLM on remote node timed out or disconnected. Details: ${err.message}`);
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
    handledTaskIdsRef.current = new Set();
    const newId = crypto.randomUUID();
    const storageKey = `krastix_session_id_${selectedDomain}`;
    localStorage.setItem(storageKey, newId);
    setSessionId(newId);
  };

  const switchChat = (id) => {
    // Determine the domain of the chat we're switching to
    const chat = recentChats.find(c => c.id === id);
    if (chat && chat.domain_key !== selectedDomain) {
       setSelectedDomain(chat.domain_key);
       localStorage.setItem('krastix_selected_domain', chat.domain_key);
    }
    setMessages([]);
    setPendingTasks([]);
    handledTaskIdsRef.current = new Set();
    setSessionId(id);
    const storageKey = `krastix_session_id_${chat?.domain_key || selectedDomain}`;
    localStorage.setItem(storageKey, id);
  };

  const domainLabel = domains.find((d) => d.domain_key === selectedDomain)?.display_name || selectedDomain;

  return (
    <div className="flex h-screen bg-[#0d0d14] text-white overflow-hidden relative">
      {/* Integrations Modal */}
      {showIntegrations && (
        <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
          <div className="bg-[#13131f] border border-white/10 rounded-2xl w-full max-w-md shadow-2xl overflow-hidden flex flex-col">
            <div className="px-6 py-5 border-b border-white/10 flex justify-between items-center bg-white/[0.02]">
              <h3 className="text-lg font-bold">App Integrations</h3>
              <button onClick={() => setShowIntegrations(false)} className="text-slate-400 hover:text-white">
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>
            <div className="p-6 space-y-6">
              {/* Google Integration */}
              <div className="bg-white/[0.02] border border-white/5 rounded-xl p-4">
                <div className="flex justify-between items-center mb-3">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-lg bg-blue-500/20 text-blue-400 flex items-center justify-center font-bold text-xl">G</div>
                    <div>
                      <h4 className="font-semibold text-sm">Google Gmail</h4>
                      <p className="text-xs text-slate-400">OAuth sign-in for email sending</p>
                    </div>
                  </div>
                  {activeIntegrations.includes('google') ? (
                    <span className="text-xs font-semibold text-emerald-400 bg-emerald-400/10 px-2 py-1 rounded">Connected</span>
                  ) : (
                    <span className="text-xs font-semibold text-slate-500">Not Connected</span>
                  )}
                </div>

                {!activeIntegrations.includes('google') ? (
                  <button
                    onClick={connectGoogle}
                    className="bg-white text-black font-semibold text-sm px-4 py-2 rounded-lg hover:bg-slate-200"
                  >
                    Connect with Google
                  </button>
                ) : (
                  <p className="text-xs text-emerald-300">
                    Gmail access is active. Drafts will be sent from this signed-in account after approval.
                  </p>
                )}
              </div>

              {/* Tally Integration */}
              <div className="bg-white/[0.02] border border-white/5 rounded-xl p-4">
                <div className="flex justify-between items-center mb-3">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-lg bg-pink-500/20 text-pink-400 flex items-center justify-center font-bold text-xl">T</div>
                    <div>
                      <h4 className="font-semibold text-sm">Tally Forms</h4>
                      <p className="text-xs text-slate-400">Generate intelligent surveys</p>
                    </div>
                  </div>
                  {activeIntegrations.includes('tally') ? (
                    <span className="text-xs font-semibold text-emerald-400 bg-emerald-400/10 px-2 py-1 rounded">Connected</span>
                  ) : (
                    <span className="text-xs font-semibold text-slate-500">Not Connected</span>
                  )}
                </div>
                {!activeIntegrations.includes('tally') && (
                  <div className="flex gap-2">
                    <input type="password" placeholder="Tally Personal Access Token..." value={integrationTokens.tally} onChange={(e) => setIntegrationTokens(prev => ({ ...prev, tally: e.target.value }))} className="flex-1 bg-[#0d0d14] border border-white/10 rounded-lg px-3 py-2 text-sm focus:ring-1 focus:ring-pink-500" />
                    <button onClick={() => saveIntegration('tally')} className="bg-white text-black font-semibold text-sm px-4 py-2 rounded-lg hover:bg-slate-200">Save</button>
                  </div>
                )}

                {activeIntegrations.includes('tally') && (
                  <div className="mt-4 border-t border-white/10 pt-3">
                    <div className="flex items-center justify-between mb-2">
                      <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">Active Tally Forms</p>
                      <button
                        onClick={fetchTallyForms}
                        className="text-[11px] text-pink-300 hover:text-pink-200"
                        disabled={loadingTallyForms}
                      >
                        {loadingTallyForms ? 'Refreshing...' : 'Refresh'}
                      </button>
                    </div>
                    <div className="max-h-44 overflow-y-auto space-y-2 pr-1">
                      {loadingTallyForms && (
                        <p className="text-xs text-slate-500">Loading forms...</p>
                      )}
                      {!loadingTallyForms && tallyForms.length === 0 && (
                        <p className="text-xs text-slate-500">No forms found for this Tally account.</p>
                      )}
                      {!loadingTallyForms && tallyForms.map((form) => (
                        <div key={form.id || form.url} className="bg-[#0d0d14] border border-white/10 rounded-lg px-3 py-2">
                          <p className="text-xs text-white font-medium truncate">{form.title || 'Untitled Form'}</p>
                          <p className="text-[11px] text-slate-400 truncate">{form.url}</p>
                          <button
                            onClick={() => {
                              setInputMessage(`Get me the applicants list for this form ${form.url}`);
                              setShowIntegrations(false);
                            }}
                            className="mt-2 text-[11px] bg-pink-500/20 border border-pink-500/40 text-pink-300 px-2 py-1 rounded hover:bg-pink-500/30"
                          >
                            Use For Applicants Query
                          </button>
                        </div>
                      ))}
                    </div>

                    <div className="mt-4 border-t border-white/10 pt-3">
                      <div className="flex items-center justify-between mb-2 gap-2">
                        <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">Stored Applicants</p>
                        <div className="flex items-center gap-2">
                          <select
                            value={applicantFormFilter}
                            onChange={(e) => setApplicantFormFilter(e.target.value)}
                            className="bg-[#0d0d14] border border-white/10 rounded-md px-2 py-1 text-[11px] text-slate-300"
                          >
                            <option value="">All Forms</option>
                            {tallyForms.map((f) => (
                              <option key={f.id || f.url} value={f.id || ''}>
                                {(f.title || 'Untitled').slice(0, 24)}{(f.title || '').length > 24 ? '...' : ''}
                              </option>
                            ))}
                          </select>
                          <button
                            onClick={() => fetchStoredApplicants(applicantFormFilter)}
                            className="text-[11px] text-pink-300 hover:text-pink-200"
                            disabled={loadingStoredApplicants}
                          >
                            {loadingStoredApplicants ? 'Refreshing...' : 'Refresh'}
                          </button>
                        </div>
                      </div>

                      <div className="max-h-52 overflow-auto border border-white/10 rounded-lg">
                        <table className="w-full text-[11px]">
                          <thead className="bg-white/[0.03] text-slate-400 sticky top-0">
                            <tr>
                              <th className="text-left px-2 py-1.5 font-medium">Form</th>
                              <th className="text-left px-2 py-1.5 font-medium">Response</th>
                              <th className="text-left px-2 py-1.5 font-medium">Submitted</th>
                            </tr>
                          </thead>
                          <tbody>
                            {loadingStoredApplicants && (
                              <tr>
                                <td colSpan={3} className="px-2 py-3 text-slate-500">Loading cached applicants...</td>
                              </tr>
                            )}
                            {!loadingStoredApplicants && storedApplicants.length === 0 && (
                              <tr>
                                <td colSpan={3} className="px-2 py-3 text-slate-500">No cached applicant submissions yet.</td>
                              </tr>
                            )}
                            {!loadingStoredApplicants && storedApplicants.map((row) => (
                              <tr key={row.id} className="border-t border-white/5 text-slate-300 hover:bg-white/[0.02]">
                                <td className="px-2 py-1.5 align-top">{row.source_form_id || '-'}</td>
                                <td className="px-2 py-1.5 align-top">{row.response_id || '-'}</td>
                                <td className="px-2 py-1.5 align-top">
                                  {row.submitted_at ? new Date(row.submitted_at).toLocaleString() : '-'}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Jotform Integration */}
              <div className="bg-white/[0.02] border border-white/5 rounded-xl p-4">
                <div className="flex justify-between items-center mb-3">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-lg bg-orange-500/20 text-orange-400 flex items-center justify-center font-bold text-xl">J</div>
                    <div>
                      <h4 className="font-semibold text-sm">Jotform</h4>
                      <p className="text-xs text-slate-400">Enterprise form builder</p>
                    </div>
                  </div>
                  {activeIntegrations.includes('jotform') ? (
                    <span className="text-xs font-semibold text-emerald-400 bg-emerald-400/10 px-2 py-1 rounded">Connected</span>
                  ) : (
                    <span className="text-xs font-semibold text-slate-500">Not Connected</span>
                  )}
                </div>
                {!activeIntegrations.includes('jotform') && (
                  <div className="flex gap-2">
                    <input type="password" placeholder="Jotform API Key..." value={integrationTokens.jotform} onChange={(e) => setIntegrationTokens(prev => ({ ...prev, jotform: e.target.value }))} className="flex-1 bg-[#0d0d14] border border-white/10 rounded-lg px-3 py-2 text-sm focus:ring-1 focus:ring-orange-500" />
                    <button onClick={() => saveIntegration('jotform')} className="bg-white text-black font-semibold text-sm px-4 py-2 rounded-lg hover:bg-slate-200">Save</button>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── Sidebar ── */}
      <aside
        className={`flex flex-col bg-[#13131f] border-r border-white/8 transition-all duration-300 ${sidebarOpen ? 'w-64' : 'w-0 overflow-hidden'
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
              onChange={(e) => {
                const val = e.target.value;
                setSelectedDomain(val);
                localStorage.setItem('krastix_selected_domain', val);
              }}
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

          {/* Integrations */}
          <button
            onClick={() => setShowIntegrations(true)}
            className="w-full flex items-center gap-2 bg-pink-600/20 hover:bg-pink-600/30 border border-pink-500/30 text-pink-300 text-sm font-medium rounded-xl px-3 py-2.5 transition-all"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m13.35-.622l1.757-1.757a4.5 4.5 0 00-6.364-6.364l-4.5 4.5a4.5 4.5 0 001.242 7.244" />
            </svg>
            App Integrations
          </button>

          {/* Pending Tasks */}
          {pendingTasks.length > 0 && <TaskBadge count={pendingTasks.length} />}

          {/* Recent Chats */}
          <div>
            <label className="text-xs font-medium text-slate-500 uppercase tracking-widest mb-3 block">
              Recent Conversations
            </label>
            <div className="space-y-1.5">
              {recentChats.length > 0 ? (
                recentChats.map((chat) => (
                  <div
                    key={chat.id}
                    onClick={() => switchChat(chat.id)}
                    className={`group relative flex flex-col gap-1 px-3 py-2.5 rounded-xl text-sm transition-all cursor-pointer border ${
                      sessionId === chat.id 
                        ? 'bg-indigo-600/10 border-indigo-500/30 text-indigo-300' 
                        : 'bg-white/[0.02] border-white/5 text-slate-400 hover:bg-white/5 hover:border-white/10 hover:text-slate-200'
                    }`}
                  >
                    <div className="flex justify-between items-start">
                      <span className="font-medium truncate pr-4">
                        {chat.id === sessionId ? "Active Research" : `Session ${chat.id.slice(0, 8)}`}
                      </span>
                      <span className="text-[10px] text-slate-500 whitespace-nowrap">
                        {new Date(chat.updated_at).toLocaleDateString()}
                      </span>
                    </div>
                    {chat.domain_key && (
                      <span className="text-[10px] uppercase tracking-tighter opacity-50 font-bold">
                        {chat.domain_key}
                      </span>
                    )}
                    {sessionId === chat.id && (
                      <div className="absolute left-0 top-1/4 bottom-1/4 w-0.5 bg-indigo-500 rounded-full" />
                    )}
                  </div>
                ))
              ) : (
                <div className="px-1 py-4 text-center border border-dashed border-white/5 rounded-xl">
                  <p className="text-xs text-slate-600 italic">No history in this domain</p>
                </div>
              )}
            </div>
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
                      className={`rounded-2xl px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap break-words ${msg.role === 'user'
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

              {/* Background task processing indicator */}
              {!loading && pendingTasks.length > 0 && (
                <div className="flex gap-3">
                  <div className="w-8 h-8 rounded-full bg-gradient-to-br from-amber-500 to-orange-600 flex items-center justify-center flex-shrink-0 animate-pulse">
                    <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182M2.985 19.644l3.182-3.182" />
                    </svg>
                  </div>
                  <div className="bg-gradient-to-r from-amber-500/10 to-orange-500/10 border border-amber-500/20 rounded-2xl rounded-tl-sm px-4 py-3 max-w-[75%]">
                    <div className="flex items-center gap-2 text-sm text-amber-300">
                      <span className="relative flex h-2 w-2">
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
                        <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-400" />
                      </span>
                      <span className="font-medium">Processing background task…</span>
                    </div>
                    <p className="text-xs text-slate-400 mt-1">Your delegated task is running. Results will appear here automatically.</p>
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