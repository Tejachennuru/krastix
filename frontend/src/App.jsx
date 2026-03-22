import React from 'react';
import { useAuth } from './context/AuthContext';
import AuthPage from './pages/AuthPage';
import ChatPage from './pages/ChatPage';

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }
  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }
  componentDidCatch(error, errorInfo) {
    this.setState({ errorInfo });
    console.error("ErrorBoundary caught an error", error, errorInfo);
  }
  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: '20px', background: '#ffdddd', color: 'black', minHeight: '100vh', fontFamily: 'monospace' }}>
          <h2>💥 Frontend Component Crash</h2>
          <details style={{ whiteSpace: 'pre-wrap' }}>
            {this.state.error && this.state.error.toString()}
            <br />
            {this.state.errorInfo && this.state.errorInfo.componentStack}
          </details>
        </div>
      );
    }
    return this.props.children;
  }
}

function MainApp() {
  const { user } = useAuth();
  return user ? <ChatPage /> : <AuthPage />;
}

export default function App() {
  return (
    <ErrorBoundary>
      <MainApp />
    </ErrorBoundary>
  );
}