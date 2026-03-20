import { useAuth } from './context/AuthContext';
import AuthPage from './pages/AuthPage';
import ChatPage from './pages/ChatPage';

function App() {
  const { user } = useAuth();
  return user ? <ChatPage /> : <AuthPage />;
}

export default App;