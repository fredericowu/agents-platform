import { useEffect, useState } from "react";
import { NavLink, Route, Routes, Navigate, useSearchParams, useLocation } from "react-router-dom";
import { wsManager } from "./lib/ws";
import { Menu, X } from "lucide-react";
import Dashboard from "./routes/Dashboard";
import Agents from "./routes/Agents";
import AgentEdit from "./routes/AgentEdit";
import AgentConfigs from "./routes/AgentConfigs";
import AgentConfigEdit from "./routes/AgentConfigEdit";
import AgentGroups from "./routes/AgentGroups";
import AgentGroupEdit from "./routes/AgentGroupEdit";
import Workflows from "./routes/Workflows";
import WorkflowEdit from "./routes/WorkflowEdit";
import AgentsFlows from "./routes/AgentsFlows";
import AgentFlowEdit from "./routes/AgentFlowEdit";
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
import RemoteAgents from "./routes/RemoteAgents";
import Sessions from "./routes/Sessions";
import {
  LayoutDashboard, Bot, Workflow as WfIcon, MessageCircle,
  Activity, Cpu, Plug, Sparkles, GaugeCircle, Settings as SettingsIcon,
  Crosshair, BookOpen, Monitor, TerminalSquare, Settings2, Share2, Users,
} from "lucide-react";

const NAV = [
  { path: "/", label: "Dashboard",   icon: LayoutDashboard, exact: true },
  { path: "/targets", label: "Targets", icon: Crosshair },
  { path: "/lessons", label: "Lessons", icon: BookOpen },
  { path: "/agents", label: "Agents", icon: Bot },
  { path: "/agent-groups", label: "Agent Group", icon: Users },
  { path: "/agents-flow", label: "Agents Flow", icon: Share2 },
  { path: "/agent-configs", label: "Agents Config", icon: Settings2 },
  { path: "/workflows", label: "Workflows", icon: WfIcon },
  { path: "/playground", label: "Playground", icon: MessageCircle },
  { path: "/runs", label: "Runs", icon: Activity },
  { path: "/evals", label: "Evals", icon: GaugeCircle },
  { path: "/models", label: "Models", icon: Cpu },
  { path: "/mcp", label: "MCP", icon: Plug },
  { path: "/skills", label: "Skills", icon: Sparkles },
  { path: "/sessions", label: "Sessions", icon: TerminalSquare },
  { path: "/remote-agents", label: "Remote Agents", icon: Monitor },
  { path: "/settings", label: "Settings", icon: SettingsIcon },
];

export default function App() {
  // Embedded/clean mode (e.g. Telegram "View progress" deep-link):
  // ?view=telegram hides the sidebar so a single run fills the viewport.
  const [params] = useSearchParams();
  const embedded = params.get("view") === "telegram";
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();

  useEffect(() => {
    wsManager.connect();
    return () => wsManager.disconnect();
  }, []);

  // Close the mobile drawer whenever the route changes.
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  const navLinks = (
    <nav className="flex-1 py-2 overflow-y-auto">
      {NAV.map(({ path, label, icon: Icon, exact }) => (
        <NavLink
          key={path}
          to={path}
          end={!!exact}
          className={({ isActive }) =>
            `flex items-center gap-3 px-4 py-3 md:py-2 text-sm border-l-2 ${
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
  );

  return (
    <div className="flex flex-col md:flex-row h-full">
      {!embedded && (
      <>
        {/* Mobile top bar with hamburger toggle */}
        <div className="md:hidden flex items-center justify-between px-4 py-3 border-b border-line bg-bg-2">
          <button
            type="button"
            aria-label={navOpen ? "Close navigation" : "Open navigation"}
            onClick={() => setNavOpen(v => !v)}
            className="text-fg p-1 -ml-1"
          >
            {navOpen ? <X size={22} /> : <Menu size={22} />}
          </button>
          <span className="text-sm font-semibold text-accent">
            {NAV.find(n => (n.exact ? location.pathname === n.path : location.pathname.startsWith(n.path)))?.label ?? "Agents Platform"}
          </span>
          <span className="w-[22px]" />
        </div>

        {/* Desktop sidebar */}
        <aside className="hidden md:flex w-56 shrink-0 border-r border-line bg-bg-2 flex-col">
          {navLinks}
        </aside>

        {/* Mobile drawer overlay */}
        {navOpen && (
          <div className="md:hidden fixed inset-0 z-50 flex">
            <div className="w-64 max-w-[80vw] bg-bg-2 border-r border-line flex flex-col">
              {navLinks}
            </div>
            <div
              className="flex-1 bg-black/50"
              onClick={() => setNavOpen(false)}
            />
          </div>
        )}
      </>
      )}
      <main className="flex-1 overflow-auto min-w-0">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/agents/:slug" element={<AgentEdit />} />
          <Route path="/agent-groups" element={<AgentGroups />} />
          <Route path="/agent-groups/:slug" element={<AgentGroupEdit />} />
          <Route path="/agents-flow" element={<AgentsFlows />} />
          <Route path="/agents-flow/:slug" element={<AgentFlowEdit />} />
          <Route path="/agent-configs" element={<AgentConfigs />} />
          <Route path="/agent-configs/:slug" element={<AgentConfigEdit />} />
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
          <Route path="/sessions" element={<Sessions />} />
          <Route path="/remote-agents" element={<RemoteAgents />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
