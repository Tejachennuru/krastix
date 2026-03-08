import React, { useState, useEffect, useRef, useCallback } from 'react';

const API_URL = 'http://localhost:8000';
const TEST_USER_ID = '7317cbb9-65f7-423e-a194-5f30d3125e26';

function App() {
  const [domains, setDomains] = useState([]);
  const [selectedDomain, setSelectedDomain] = useState('');
  const [messages, setMessages] = useState([]);
  const [inputMessage, setInputMessage] = useState('');
  const [conversationId, setConversationId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [pendingTasks, setPendingTasks] = useState([]);
  const [activeTools, setActiveTools] = useState([]);
  const messagesEndRef = useRef(null);
  const abortControllerRef = useRef(null);

  useEffect(() => {
    fetchDomains();
  }, []);

  useEffect(() => {
    if (pendingTasks.length > 0) {
      const interval = setInterval(checkPendingTasks, 5000);
      return () => clearInterval(interval);
    }
  }, [pendingTasks]);

  useEffect(() => {
    scrollToBottom();
  }, [messages, streamingText]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const fetchDomains = async () => {
    try {
      const response = await fetch(`${API_URL}/domains`);
      const data = await response.json();
      setDomains(data);
      if (data.length > 0) setSelectedDomain(data[0].domain_key);
    } catch (error) {
      console.error('Error fetching domains:', error);
    }
  };

  const checkPendingTasks = async () => {
    const updatedTasks = [];
    for (const taskId of pendingTasks) {
      try {
        const response = await fetch(`${API_URL}/task/${taskId}?user_id=${TEST_USER_ID}`);
        const data = await response.json();
        if (data.status === 'completed') {
          setMessages(prev => [...prev, {
            role: 'system',
            content: `Task completed: ${JSON.stringify(data.result, null, 2)}`,
            type: 'task_complete',
          }]);
        } else if (data.status === 'failed') {
          setMessages(prev => [...prev, {
            role: 'system',
            content: `Task failed: ${data.error}`,
            type: 'task_failed',
          }]);
        } else {
          updatedTasks.push(taskId);
        }
      } catch (error) {
        updatedTasks.push(taskId);
      }
    }
    setPendingTasks(updatedTasks);
  };

  const sendMessageStreaming = useCallback(async () => {
    if (!inputMessage.trim() || !selectedDomain) return;

    const userMessage = { role: 'user', content: inputMessage };
    const currentInput = inputMessage;
    const sessionId = conversationId || `session-${Date.now()}`;
    if (!conversationId) setConversationId(sessionId);

    setMessages(prev => [...prev, userMessage]);
    setInputMessage('');
    setLoading(true);
    setStreamingText('');
    setActiveTools([]);

    // Abort any previous stream
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    abortControllerRef.current = new AbortController();

    try {
      const response = await fetch(`${API_URL}/api/v1/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: TEST_USER_ID,
          domain: selectedDomain,
          message: currentInput,
          session_id: sessionId,
        }),
        signal: abortControllerRef.current.signal,
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let accumulated = '';
      let buffer = '';
      let gotDone = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        let currentEvent = '';
        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim();
          } else if (line.startsWith('data: ')) {
            const rawData = line.slice(6);
            try {
              const data = JSON.parse(rawData);
              
              switch (currentEvent) {
                case 'token':
                  accumulated += data;
                  setStreamingText(accumulated);
                  break;

                case 'tool_start':
                  setActiveTools(prev => [...prev, data.tool]);
                  break;

                case 'tool_result':
                  setActiveTools([]);
                  setMessages(prev => [...prev, {
                    role: 'system',
                    content: data,
                    type: 'tool_result',
                  }]);
                  break;

                case 'done':
                  gotDone = true;
                  const finalResponse = data.response || accumulated;
                  setStreamingText('');
                  setMessages(prev => [...prev, {
                    role: 'assistant',
                    content: finalResponse,
                  }]);
                  if (data.task_id) {
                    setPendingTasks(prev => [...prev, data.task_id]);
                  }
                  break;

                case 'error':
                  gotDone = true;
                  setStreamingText('');
                  setMessages(prev => [...prev, {
                    role: 'assistant',
                    content: `Error: ${data}`,
                    type: 'error',
                  }]);
                  break;
              }
            } catch (e) {
              // Skip malformed JSON
            }
            currentEvent = '';
          }
        }
      }

      // If stream ended without 'done' event, commit accumulated text
      if (accumulated && !gotDone) {
        setStreamingText('');
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: accumulated,
        }]);
      }

    } catch (error) {
      if (error.name !== 'AbortError') {
        // Fallback: try non-streaming endpoint
        try {
          const response = await fetch(`${API_URL}/api/v1/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              user_id: TEST_USER_ID,
              domain: selectedDomain,
              message: currentInput,
              session_id: sessionId,
            }),
          });
          const data = await response.json();
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: data.response,
          }]);
          if (data.task_id) {
            setPendingTasks(prev => [...prev, data.task_id]);
          }
        } catch (fallbackError) {
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: `Connection error: ${fallbackError.message}`,
            type: 'error',
          }]);
        }
      }
    } finally {
      setLoading(false);
      setStreamingText('');
      setActiveTools([]);
    }
  }, [inputMessage, selectedDomain, conversationId]);

  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessageStreaming();
    }
  };

  const stopStreaming = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      setLoading(false);
      setStreamingText('');
    }
  };

  const getMessageStyle = (msg) => {
    if (msg.type === 'error') return 'bg-red-900/40 border border-red-700';
    if (msg.type === 'tool_result') return 'bg-purple-900/30 border border-purple-700 text-sm';
    if (msg.type === 'task_complete') return 'bg-green-900/30 border border-green-700 text-sm';
    if (msg.type === 'task_failed') return 'bg-red-900/30 border border-red-700 text-sm';
    if (msg.role === 'user') return 'bg-blue-600';
    return 'bg-gray-700';
  };

  return (
    <div className="min-h-screen bg-gray-900 text-white flex">
      {/* Sidebar */}
      <div className="w-64 bg-gray-800 border-r border-gray-700 p-4">
        <h1 className="text-xl font-bold mb-6">Krastix AI</h1>
        
        <div className="mb-6">
          <label className="block text-sm font-medium mb-2">Domain</label>
          <select
            value={selectedDomain}
            onChange={(e) => setSelectedDomain(e.target.value)}
            className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2"
          >
            {domains.map(domain => (
              <option key={domain.domain_key} value={domain.domain_key}>
                {domain.display_name}
              </option>
            ))}
          </select>
        </div>

        <button
          onClick={() => {
            setMessages([]);
            setConversationId(null);
            setPendingTasks([]);
            setStreamingText('');
          }}
          className="w-full bg-blue-600 hover:bg-blue-700 rounded px-4 py-2 text-sm"
        >
          New Conversation
        </button>

        {pendingTasks.length > 0 && (
          <div className="mt-4 p-3 bg-yellow-900/30 border border-yellow-700 rounded">
            <div className="text-xs text-yellow-400">
              {pendingTasks.length} task(s) processing...
            </div>
          </div>
        )}

        {activeTools.length > 0 && (
          <div className="mt-4 p-3 bg-purple-900/30 border border-purple-700 rounded">
            <div className="text-xs text-purple-400">
              Delegating: {activeTools.join(', ')}
            </div>
          </div>
        )}
      </div>

      {/* Chat Area */}
      <div className="flex-1 flex flex-col">
        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-6">
          {messages.length === 0 && !streamingText ? (
            <div className="text-center text-gray-400 mt-20">
              <h2 className="text-2xl font-bold mb-2">Welcome to Krastix AI</h2>
              <p>Select a domain and start chatting with your AI assistant</p>
              <p className="text-sm mt-2 text-gray-500">Responses stream in real-time</p>
            </div>
          ) : (
            <div className="max-w-4xl mx-auto space-y-4">
              {messages.map((msg, idx) => (
                <div
                  key={idx}
                  className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
                >
                  <div className={`max-w-2xl rounded-lg px-4 py-3 ${getMessageStyle(msg)}`}>
                    <div className="text-xs text-gray-300 mb-1">
                      {msg.role === 'user' ? 'You' : msg.type === 'tool_result' ? 'Agent' : 'Assistant'}
                    </div>
                    <div className="whitespace-pre-wrap">{msg.content}</div>
                  </div>
                </div>
              ))}

              {/* Streaming indicator */}
              {streamingText && (
                <div className="flex justify-start">
                  <div className="max-w-2xl rounded-lg px-4 py-3 bg-gray-700 border border-gray-600">
                    <div className="text-xs text-gray-300 mb-1">
                      Assistant
                      <span className="ml-2 inline-block w-2 h-2 bg-green-400 rounded-full animate-pulse" />
                    </div>
                    <div className="whitespace-pre-wrap">{streamingText}</div>
                  </div>
                </div>
              )}

              {/* Thinking indicator */}
              {loading && !streamingText && (
                <div className="flex justify-start">
                  <div className="bg-gray-700 rounded-lg px-4 py-3">
                    <div className="flex items-center space-x-2">
                      <div className="w-2 h-2 bg-blue-500 rounded-full animate-bounce"></div>
                      <div className="w-2 h-2 bg-blue-500 rounded-full animate-bounce" style={{animationDelay: '0.1s'}}></div>
                      <div className="w-2 h-2 bg-blue-500 rounded-full animate-bounce" style={{animationDelay: '0.2s'}}></div>
                      <span className="text-xs text-gray-400 ml-2">Thinking...</span>
                    </div>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        {/* Input Area */}
        <div className="border-t border-gray-700 p-4">
          <div className="max-w-4xl mx-auto flex gap-2">
            <textarea
              value={inputMessage}
              onChange={(e) => setInputMessage(e.target.value)}
              onKeyDown={handleKeyPress}
              placeholder="Type your message..."
              className="flex-1 bg-gray-800 border border-gray-600 rounded px-4 py-3 resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"
              rows="2"
              disabled={loading}
            />
            {loading ? (
              <button
                onClick={stopStreaming}
                className="bg-red-600 hover:bg-red-700 rounded px-6 py-3 font-medium"
              >
                Stop
              </button>
            ) : (
              <button
                onClick={sendMessageStreaming}
                disabled={!inputMessage.trim()}
                className="bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 disabled:cursor-not-allowed rounded px-6 py-3 font-medium"
              >
                Send
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;