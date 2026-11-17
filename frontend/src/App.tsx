import React from 'react';
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { ThemeProvider } from './context/ThemeContext';
import { AuthProvider } from './context/AuthContext';
import { TopBar } from './components/layout/TopBar';
import { LiquidNavbar } from './components/layout/LiquidNavbar';
import { HomePage } from './pages/HomePage';
import { OverviewDashboard } from './pages/OverviewDashboard';
import { ConversationsView } from './pages/ConversationsView';
import { TicketManagementView } from './pages/TicketManagementView';
import { EscalationCenterView } from './pages/EscalationCenterView';
import { EvidenceProcessingView } from './pages/EvidenceProcessingView';
import { KnowledgeOpsView } from './pages/KnowledgeOpsView';
import { AnalyticsView } from './pages/AnalyticsView';
import { SettingsView } from './pages/SettingsView';

const AppContent: React.FC = () => {
  const location = useLocation();
  const isPublicHomepage = location.pathname === '/';

  if (isPublicHomepage) {
    return <HomePage />;
  }

  return (
    <div className="app-layout-wrapper" style={{ display: 'flex', minHeight: '100vh', width: '100%' }}>
      {/* Main Content Workspace (Full Width, Sidebar replaced by Floating Dock) */}
      <div
        className="app-main-layout"
        style={{
          marginLeft: 0,
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          minWidth: 0,
        }}
      >
        <TopBar />

        <main
          style={{
            flex: 1,
            padding: '28px 28px 110px 28px',
            maxWidth: '1600px',
            width: '100%',
            margin: '0 auto',
          }}
        >
          <Routes>
            <Route path="/app" element={<OverviewDashboard />} />
            <Route path="/app/overview" element={<OverviewDashboard />} />
            <Route path="/conversations" element={<ConversationsView />} />
            <Route path="/tickets" element={<TicketManagementView />} />
            <Route path="/escalations" element={<EscalationCenterView />} />
            <Route path="/app/escalations" element={<EscalationCenterView />} />
            <Route path="/evidence" element={<EvidenceProcessingView />} />
            <Route path="/knowledge" element={<KnowledgeOpsView />} />
            <Route path="/analytics" element={<AnalyticsView />} />
            <Route path="/settings" element={<SettingsView />} />
            <Route path="*" element={<Navigate to="/app" replace />} />
          </Routes>
        </main>

        {/* Floating Navigation Dock for Operational App Routes */}
        <LiquidNavbar />
      </div>
    </div>
  );
};

export const App: React.FC = () => {
  return (
    <ThemeProvider>
      <AuthProvider>
        <BrowserRouter>
          <AppContent />
        </BrowserRouter>
      </AuthProvider>
    </ThemeProvider>
  );
};

export default App;
