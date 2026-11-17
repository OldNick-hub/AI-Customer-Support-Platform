import React, {
  createContext,
  useContext,
  useEffect,
  useState,
  useMemo,
  useCallback,
} from 'react';
import {
  AuthSession,
  CurrentUser,
  authApi,
  getApiAuthSession,
  setApiAuthSession,
  clearApiAuthSession,
  setOnUnauthorized,
} from '../services/api';

export interface AuthContextType {
  activeSession: AuthSession | null;
  currentUser: CurrentUser | null;
  isLoading: boolean;
  error: string | null;
  logout: () => void;
  refreshIdentity: () => Promise<void>;
  loginAs: (persona?: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [activeSession, setActiveSession] = useState<AuthSession | null>(() => getApiAuthSession());
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const logout = useCallback(() => {
    clearApiAuthSession();
    setActiveSession(null);
    setCurrentUser(null);
    setError(null);
  }, []);

  const loginAs = useCallback(async (persona: string = 'agent') => {
    setIsLoading(true);
    setError(null);
    try {
      const session = await authApi.issueDevToken(persona);
      setApiAuthSession(session);
      setActiveSession(session);
      const user = await authApi.getCurrentUser();
      setCurrentUser(user);
    } catch (err: any) {
      setError(err?.message || 'Login failed');
    } finally {
      setIsLoading(false);
    }
  }, []);

  const refreshIdentity = useCallback(async () => {
    try {
      const user = await authApi.getCurrentUser();
      setCurrentUser(user);
      setError(null);
    } catch {
      // Identity verification failed - re-authenticate automatically
      await loginAs('agent');
    }
  }, [loginAs]);

  // Initialize AuthContext
  useEffect(() => {
    let isMounted = true;

    // Register 401 callback for automatic session invalidation & recovery
    setOnUnauthorized(() => {
      if (isMounted) {
        clearApiAuthSession();
        // Self-heal by issuing fresh dev session
        authApi.issueDevToken('agent').then((session) => {
          if (isMounted) {
            setApiAuthSession(session);
            setActiveSession(session);
            authApi.getCurrentUser().then((u) => {
              if (isMounted) setCurrentUser(u);
            }).catch(() => {});
          }
        }).catch(() => {
          if (isMounted) logout();
        });
      }
    });

    const initAuth = async () => {
      setIsLoading(true);

      const existingSession = getApiAuthSession();
      let sessionValid = false;

      if (existingSession && existingSession.token) {
        try {
          const user = await authApi.getCurrentUser();
          if (isMounted) {
            setActiveSession(existingSession);
            setCurrentUser(user);
            sessionValid = true;
          }
        } catch {
          // Expired or invalid token - will self-heal below
          clearApiAuthSession();
        }
      }

      if (!sessionValid) {
        try {
          const defaultSession = await authApi.issueDevToken('agent');
          if (isMounted) {
            setApiAuthSession(defaultSession);
            setActiveSession(defaultSession);
            const user = await authApi.getCurrentUser();
            setCurrentUser(user);
          }
        } catch (err: any) {
          console.error('Failed to issue default dev session', err);
          if (isMounted) {
            logout();
          }
        }
      }

      if (isMounted) {
        setIsLoading(false);
      }
    };

    initAuth();

    return () => {
      isMounted = false;
      setOnUnauthorized(null);
    };
  }, [logout]);

  const value = useMemo(
    () => ({
      activeSession,
      currentUser,
      isLoading,
      error,
      logout,
      refreshIdentity,
      loginAs,
    }),
    [
      activeSession,
      currentUser,
      isLoading,
      error,
      logout,
      refreshIdentity,
      loginAs,
    ]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};

export const useAuth = (): AuthContextType => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};

