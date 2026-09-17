import React, { useState, useEffect } from 'react';
import { AppProvider } from '@/context/AppContext';
import { Navbar } from '@/components/layout/Navbar';
import { Sidebar } from '@/components/layout/Sidebar';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { ServiceMonitor } from '@/components/ServiceMonitor';
import { OverviewPage } from '@/pages/OverviewPage';
import { ProjectsPage } from '@/pages/ProjectsPage';
import { AIVaultPage } from '@/pages/AIVaultPage';
import { MemoryPage } from '@/pages/MemoryPage';
import { ProjectWorkbenchPage } from '@/pages/ProjectWorkbenchPage';

// 全局（与具体项目无关）的功能放这里；单个项目的功能一律进 ProjectWorkbenchPage 的标签页。
// 「上传小说」已并入「项目中心 → 新建项目」，不再单独占一个入口。
const SECTIONS: Record<string, React.ComponentType<any>> = {
  projects: ProjectsPage,
  ai: AIVaultPage,
  memory: MemoryPage,
};

// Parse URL hash to determine active section and optional project key
function parseRoute(): { section: string; projectKey: string | null } {
  const hash = window.location.hash;
  let path = hash.replace('#', '') || '/';
  // Normalize: #/#/foo → #/foo
  if (path.startsWith('/#')) {
    path = path.slice(1);
  }
  const params = new URLSearchParams(path.split('?')[1] || '');
  const projectKey = params.get('p');
  const route = path.split('?')[0].replace(/^\//, '');
  // If ?p= is present, render project workbench; otherwise use the section name
  const section = projectKey ? 'project-workbench' : (route && SECTIONS[route] ? route : 'projects');
  return { section, projectKey };
}

function AppContent() {
  const [activeSection, setActiveSection] = useState(() => parseRoute().section);
  const [projectKey, setProjectKey] = useState<string | null>(() => parseRoute().projectKey);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  // Listen for hash changes (browser back/forward, direct URL)
  useEffect(() => {
    const handleHashChange = () => {
      const { section, projectKey: pk } = parseRoute();
      setActiveSection(section);
      setProjectKey(pk);
    };
    window.addEventListener('hashchange', handleHashChange);
    return () => window.removeEventListener('hashchange', handleHashChange);
  }, []);

  const PageComponent = useMemo(() => SECTIONS[activeSection] || OverviewPage, [activeSection]);

  return (
    <div className="flex h-screen bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-white">
      {/* Sidebar */}
      <Sidebar
        activeSection={activeSection}
        onNavigate={(id) => {
          setActiveSection(id);
          setProjectKey(null);
          window.location.hash = `#${id}`;
        }}
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
      />

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden">
        <div className="flex-1 flex flex-col overflow-hidden">
          <Navbar />
          <main className="flex-1 overflow-y-auto p-6">
            {activeSection === 'project-workbench' && projectKey ? (
              <ProjectWorkbenchPage projectKey={projectKey} />
            ) : (
              <PageComponent />
            )}
          </main>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <AppProvider>
        <AppContent />
        <ServiceMonitor />
      </AppProvider>
    </ErrorBoundary>
  );
}
