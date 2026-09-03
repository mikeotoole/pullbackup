// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { NavLink, Route, Routes, Navigate, useLocation, useNavigate } from "react-router-dom";
import { TasksList } from "./pages/TasksList";
import { TaskForm } from "./pages/TaskForm";
import { Sources } from "./pages/Sources";
import { RunDetail } from "./pages/RunDetail";
import { RunHistory } from "./pages/RunHistory";
import { Login } from "./pages/Login";
import { BrandMark } from "./components/BrandMark";
import { api, LOGIN_PATH } from "./lib/api";

export default function App() {
  const location = useLocation();
  const navigate = useNavigate();

  // The login page is its own full-height screen: showing the app nav around
  // a sign-in form implies the operator is already in.
  if (location.pathname.startsWith(LOGIN_PATH)) {
    return (
      <Routes>
        <Route path="/login" element={<Login />} />
      </Routes>
    );
  }

  async function signOut() {
    try {
      await api.logout();
    } finally {
      navigate(LOGIN_PATH, { replace: true });
    }
  }

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-border bg-panel">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-3 flex flex-wrap items-center gap-x-6 gap-y-2">
          <div className="flex items-center gap-2 font-semibold text-lg">
            <BrandMark />
            pullbackup
          </div>
          <nav className="flex gap-4 text-sm">
            <NavLink to="/tasks" className={({isActive}) => isActive ? "text-accent" : "text-muted hover:text-white"}>tasks</NavLink>
            <NavLink to="/sources" className={({isActive}) => isActive ? "text-accent" : "text-muted hover:text-white"}>sources</NavLink>
          </nav>
          <button
            type="button"
            onClick={signOut}
            title="sign out"
            className="ml-auto text-sm text-muted hover:text-white min-h-[44px] px-2"
          >
            sign out
          </button>
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
