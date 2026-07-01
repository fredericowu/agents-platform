import { useState, type ReactNode } from "react";

export interface TabDef {
  id: string;
  label: string;
  content: ReactNode;
}

export default function Tabs({
  tabs, defaultTab,
}: { tabs: TabDef[]; defaultTab?: string }) {
  const [active, setActive] = useState(defaultTab || tabs[0]?.id);
  const activeTab = tabs.find(t => t.id === active) || tabs[0];

  return (
    <div>
      <div className="flex gap-1 border-b border-line mb-4 overflow-x-auto" role="tablist">
        {tabs.map(t => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={active === t.id}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px whitespace-nowrap transition-colors ${
              active === t.id
                ? "border-accent text-accent"
                : "border-transparent text-muted hover:text-fg"
            }`}
            onClick={() => setActive(t.id)}
            data-testid={`tab-${t.id}`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel">{activeTab?.content}</div>
    </div>
  );
}
