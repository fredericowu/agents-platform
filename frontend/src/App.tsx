import { NavLink, Route, Routes, Navigate } from "react-router-dom";
import Dashboard from "./routes/Dashboard";
import Agents from "./routes/Agents";
import AgentEdit from "./routes/AgentEdit";
import Workflows from "./routes/Workflows";
import WorkflowEdit from "./routes/WorkflowEdit";
import Playground from "./routes/Playground";
import Runs from "./routes/Runs";
import RunDetail from "./routes/RunDetail";
import Targets from "./routes/Targets";
import TargetDetail from "./routes/TargetDetail";
import Models from "./routes/Models";
import McpPage from "./routes/Mcp";
import SkillsPage from "./routes/Skills";
import Evals from "./routes/Evals";
import Settings from "./routes/Settings";
import Lessons from "./routes/Lessons";
import {
  LayoutDashboard, Bot, Workflow as WfIcon, MessageCircle,
  Activity, Cpu, Plug, Sparkles, GaugeCircle, Settings as SettingsIcon,
  Crosshair, BookOpen,
} from "lucide-react";

const NAV = [
  { path: "/", label: "Dashboard",   icon: LayoutDashboard, exact: true },
  { path: "/targets", label: "Targets", icon: Crosshair },
  { path: "/lessons", label: "Lessons", icon: BookOpen },
  { path: "/agents", label: "Agents", icon: Bot },
  { path: "/workflows", label: "Workflows", icon: WfIcon },
  { path: "/playground", label: "Playground", icon: MessageCircle },
  { path: "/runs", label: "Runs", icon: Activity },
  { path: "/evals", label: "Evals", icon: GaugeCircle },
  { path: "/models", label: "Models", icon: Cpu },
  { path: "/mcp", label: "MCP", icon: Plug },
  { path: "/skills", label: "Skills", icon: Sparkles },
  { path: "/settings", label: "Settings", icon: SettingsIcon },
];

export default function App() {
  return (
    <div className="flex h-full">
      <aside className="w-56 border-r border-line bg-bg-2 flex flex-col">
        <nav className="flex-1 py-2">
          {NAV.map(({ path, label, icon: Icon, exact }) => (
            <NavLink
              key={path}
              to={path}
              end={!!exact}
              className={({ isActive }) =>
                `flex items-center gap-3 px-4 py-2 text-sm border-l-2 ${
                  isActive
                    ? "border-accent text-fg bg-bg-3/60"
                    : "border-transparent text-muted hover:text-fg hover:bg-bg-3/40"
                }`
              }
            >
              <Icon size={16} /> {label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="flex-1 overflow-auto">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/agents/:slug" element={<AgentEdit />} />
          <Route path="/workflows" element={<Workflows />} />
          <Route path="/workflows/:slug" element={<WorkflowEdit />} />
          <Route path="/playground" element={<Playground />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/targets" element={<Targets />} />
          <Route path="/targets/:slug" element={<TargetDetail />} />
          <Route path="/lessons" element={<Lessons />} />
          <Route path="/evals" element={<Evals />} />
          <Route path="/models" element={<Models />} />
          <Route path="/mcp" element={<McpPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
