import React, { useState, useEffect, useRef } from 'react';

const API_URL = 'http://localhost:8000';
const TEST_USER_ID = '7317cbb9-65f7-423e-a194-5f30d3125e26'; // Use actual UUID from database

function App() {
  const [domains, setDomains] = useState([]);
  const [selectedDomain, setSelectedDomain] = useState('');
  const [messages, setMessages] = useState([]);
  const [inputMessage, setInputMessage] = useState('');
  const [conversationId, setConversationId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [pendingTasks, setPendingTasks] = useState([]);
  const messagesEndRef = useRef(null);

  useEffect(() => {
    fetchDomains();
  }, []);

  useEffect(() => {
    if (pendingTasks.length > 0) {
      const interval = setInterval(() => {
        checkPendingTasks();
      }, 2000);
      return () => clearInterval(interval);
    }
  }, [pendingTasks]);

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const fetchDomains = async () => {
    try {
      const response = await fetch(`${API_URL}/domains`);
      const data = await response.json();
      setDomains(data);
      if (data.length > 0) {
        setSelectedDomain(data[0].domain_key);
      }
    } catch (error) {
      console.error('Error fetching domains:', error);
    }
  };

  const checkPendingTasks = async () => {
    const updatedTasks = [];
    
    for (const taskId of pendingTasks) {
      try {
        const response = await fetch(
          `${API_URL}/task/${taskId}?user_id=${TEST_USER_ID}`
        );
        const data = await response.json();
        
        if (data.status === 'completed') {
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: `Task completed: ${JSON.stringify(data.result, null, 2)}`
          }]);
        } else if (data.status === 'failed') {
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: `Task failed: ${data.error}`
          }]);
        } else {
          updatedTasks.push(taskId);
        }
      } catch (error) {
        console.error('Error checking task:', error);
        updatedTasks.push(taskId);
      }
    }
    
    setPendingTasks(updatedTasks);
  };

  const sendMessage = async () => {
    if (!inputMessage.trim() || !selectedDomain) return;

    const userMessage = { role: 'user', content: inputMessage };
    setMessages(prev => [...prev, userMessage]);
    setInputMessage('');
    setLoading(true);

    try {
      const response = await fetch(`${API_URL}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: TEST_USER_ID,
          domain_key: selectedDomain,
          message: inputMessage,
          conversation_id: conversationId
        })
      });

      const data = await response.json();
      setConversationId(data.conversation_id);

      setMessages(prev => [...prev, {
        role: 'assistant',
        content: data.response
      }]);

      if (data.pending_tasks && data.pending_tasks.length > 0) {
        setPendingTasks(prev => [...prev, ...data.pending_tasks]);
      }
    } catch (error) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `Error: ${error.message}`
      }]);
    } finally {
      setLoading(false);
    }
  };

  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  return (
    <div className="min-h-screen bg-gray-900 text-white flex">
      {/* Sidebar */}
      <div className="w-64 bg-gray-800 border-r border-gray-700 p-4">
        <h1 className="text-xl font-bold mb-6">Orchestrator AI</h1>
        
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
      </div>

      {/* Chat Area */}
      <div className="flex-1 flex flex-col">
        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-6">
          {messages.length === 0 ? (
            <div className="text-center text-gray-400 mt-20">
              <h2 className="text-2xl font-bold mb-2">Welcome to Orchestrator AI</h2>
              <p>Select a domain and start chatting with your AI assistant</p>
            </div>
          ) : (
            <div className="max-w-4xl mx-auto space-y-4">
              {messages.map((msg, idx) => (
                <div
                  key={idx}
                  className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
                >
                  <div
                    className={`max-w-2xl rounded-lg px-4 py-3 ${
                      msg.role === 'user'
                        ? 'bg-blue-600'
                        : 'bg-gray-700'
                    }`}
                  >
                    <div className="text-xs text-gray-300 mb-1">
                      {msg.role === 'user' ? 'You' : 'Assistant'}
                    </div>
                    <div className="whitespace-pre-wrap">{msg.content}</div>
                  </div>
                </div>
              ))}
              {loading && (
                <div className="flex justify-start">
                  <div className="bg-gray-700 rounded-lg px-4 py-3">
                    <div className="flex space-x-2">
                      <div className="w-2 h-2 bg-blue-500 rounded-full animate-bounce"></div>
                      <div className="w-2 h-2 bg-blue-500 rounded-full animate-bounce delay-100"></div>
                      <div className="w-2 h-2 bg-blue-500 rounded-full animate-bounce delay-200"></div>
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
              onKeyPress={handleKeyPress}
              placeholder="Type your message..."
              className="flex-1 bg-gray-800 border border-gray-600 rounded px-4 py-3 resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"
              rows="2"
            />
            <button
              onClick={sendMessage}
              disabled={loading || !inputMessage.trim()}
              className="bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 disabled:cursor-not-allowed rounded px-6 py-3 font-medium"
            >
              Send
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;