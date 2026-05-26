import { NavLink, Route, Routes, Navigate } from "react-router-dom";
import { TasksList } from "./pages/TasksList";
import { TaskForm } from "./pages/TaskForm";
import { Sources } from "./pages/Sources";
import { RunDetail } from "./pages/RunDetail";

export default function App() {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-border bg-panel">
        <div className="max-w-7xl mx-auto px-6 py-3 flex items-center gap-6">
          <div className="font-semibold text-lg">pullback</div>
          <nav className="flex gap-4 text-sm">
            <NavLink to="/tasks" className={({isActive}) => isActive ? "text-accent" : "text-muted hover:text-white"}>Rsync Tasks</NavLink>
            <NavLink to="/sources" className={({isActive}) => isActive ? "text-accent" : "text-muted hover:text-white"}>Sources</NavLink>
          </nav>
        </div>
      </header>
      <main className="flex-1 max-w-7xl w-full mx-auto px-6 py-6">
        <Routes>
          <Route path="/" element={<Navigate to="/tasks" replace />} />
          <Route path="/tasks" element={<TasksList />} />
          <Route path="/tasks/new" element={<TaskForm />} />
          <Route path="/tasks/:id/edit" element={<TaskForm />} />
          <Route path="/sources" element={<Sources />} />
          <Route path="/runs/:id" element={<RunDetail />} />
        </Routes>
      </main>
    </div>
  );
}
