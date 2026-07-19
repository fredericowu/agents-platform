export const PERMISSION_DEFS: { key: string; label: string; description: string; defaultOn?: boolean }[] = [
  { key: "docker", label: "Docker", description: "Mount the Docker socket — enables docker ps, docker run, etc." },
  { key: "github", label: "GitHub / Git", description: "Mount ~/.gitconfig and ~/.config/gh (gh CLI auth) — enables gh repo list/pr/issue and HTTPS git operations." },
  {
    key: "share_network", label: "Share network",
    description: "Join aw-sandbox's network namespace instead of an isolated bridge — lets the agent reach 127.0.0.1 ports on the host (awserv, redis, postgres, agents-platform itself). Off by default — secure by default, opt in explicitly.",
  },
  {
    key: "workspace_access", label: "Agentic Workspace Folder Access",
    description: "Mount /opt/agentic-workspace into the container so the agent can read/edit the whole AW repo. Off = the agent runs isolated (only its own cwd + /tmp), with no access to the workspace. Off by default — secure by default, opt in explicitly.",
  },
  {
    key: "tmp_access", label: "/tmp access",
    description: "Bind-mount aw-sandbox's own /tmp into the container at /tmp — lets the agent read/write scratch files shared with aw-sandbox and its other processes (logs, pids, etc). Off by default: without this permission the agent's /tmp is a private, empty, per-container scratch space that vanishes when the container exits — no sandbox files are visible there.",
  },
  {
    key: "verbose_replies", label: "Verbose replies",
    description: "Deliver the full run transcript (narration + tool results, not just the post-last-tool-call text) as the chat reply, instead of truncating to the final segment. Off by default — still one message when on, just untruncated. Turn on for an agent if you want its narration visible in chat.",
  },
];
