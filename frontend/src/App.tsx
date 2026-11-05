import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { ThemeProvider } from './context/ThemeContext';
import { Sidebar } from './components/layout/Sidebar';
import { TopBar } from './components/layout/TopBar';
import { OverviewDashboard } from './pages/OverviewDashboard';
import { ConversationsView } from './pages/ConversationsView';
import { TicketManagementView } from './pages/TicketManagementView';
import { EscalationCenterView } from './pages/EscalationCenterView';
import { EvidenceProcessingView } from './pages/EvidenceProcessingView';
import { KnowledgeOpsView } from './pages/KnowledgeOpsView';
import { AnalyticsView } from './pages/AnalyticsView';
import { SettingsView } from './pages/SettingsView';

export const App: React.FC = () => {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <div className="app-layout-wrapper" style={{ display: 'flex', minHeight: '100vh', width: '100%' }}>
          {/* Persistent Glass Sidebar */}
          <Sidebar />

          {/* Main Content Workspace */}
          <div
            className="app-main-layout"
            style={{
              marginLeft: '260px',
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              minWidth: 0,
            }}
          >
            <TopBar />

            <main style={{ flex: 1, padding: '28px', maxWidth: '1600px', width: '100%', margin: '0 auto' }}>
              <Routes>
                <Route path="/" element={<OverviewDashboard />} />
                <Route path="/conversations" element={<ConversationsView />} />
                <Route path="/tickets" element={<TicketManagementView />} />
                <Route path="/escalations" element={<EscalationCenterView />} />
                <Route path="/evidence" element={<EvidenceProcessingView />} />
                <Route path="/knowledge" element={<KnowledgeOpsView />} />
                <Route path="/analytics" element={<AnalyticsView />} />
                <Route path="/settings" element={<SettingsView />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </main>
          </div>
        </div>
      </BrowserRouter>
    </ThemeProvider>
  );
};

export default App;

