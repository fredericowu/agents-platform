export const PERMISSION_DEFS: { key: string; label: string; description: string; defaultOn?: boolean }[] = [
  { key: "docker", label: "Docker", description: "Mount the Docker socket — enables docker ps, docker run, etc." },
  { key: "github", label: "GitHub / Git", description: "Mount ~/.gitconfig and ~/.config/gh (gh CLI auth) — enables gh repo list/pr/issue and HTTPS git operations." },
  {
    key: "share_network", label: "Share network", defaultOn: true,
    description: "Join aw-sandbox's network namespace instead of an isolated bridge — lets the agent reach 127.0.0.1 ports on the host (awserv, redis, postgres, agents-platform itself). On by default for now.",
  },
  {
    key: "workspace_access", label: "Agentic Workspace Folder Access", defaultOn: true,
    description: "Mount /opt/agentic-workspace into the container so the agent can read/edit the whole AW repo. Off = the agent runs isolated (only its own cwd + /tmp), with no access to the workspace.",
  },
];
