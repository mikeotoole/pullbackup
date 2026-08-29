import { NavLink, Route, Routes, Navigate } from "react-router-dom";
import { TasksList } from "./pages/TasksList";
import { TaskForm } from "./pages/TaskForm";
import { Sources } from "./pages/Sources";
import { RunDetail } from "./pages/RunDetail";
import { RunHistory } from "./pages/RunHistory";

export default function App() {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-border bg-panel">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-3 flex flex-wrap items-center gap-x-6 gap-y-2">
          <div className="flex items-center gap-2 font-semibold text-lg">
            <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true">
              <rect width="32" height="32" rx="7" fill="#7c3aed" />
              <g fill="none" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
                <path d="M16 5.5 V16.5" />
                <path d="M10.8 11.7 L16 16.9 L21.2 11.7" />
                <path d="M8 20.5 V25 H24 V20.5" />
              </g>
            </svg>
            pullbackup
          </div>
          <nav className="flex gap-4 text-sm">
            <NavLink to="/tasks" className={({isActive}) => isActive ? "text-accent" : "text-muted hover:text-white"}>tasks</NavLink>
            <NavLink to="/sources" className={({isActive}) => isActive ? "text-accent" : "text-muted hover:text-white"}>sources</NavLink>
          </nav>
        </div>
      </header>
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 py-6">
        <Routes>
          <Route path="/" element={<Navigate to="/tasks" replace />} />
          <Route path="/tasks" element={<TasksList />} />
          <Route path="/tasks/new" element={<TaskForm />} />
          <Route path="/tasks/:id/edit" element={<TaskForm />} />
          <Route path="/sources" element={<Sources />} />
          <Route path="/tasks/:id/runs" element={<RunHistory />} />
          <Route path="/runs/:id" element={<RunDetail />} />
        </Routes>
      </main>
    </div>
  );
}
