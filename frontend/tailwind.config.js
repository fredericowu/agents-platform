/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx,js,jsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg:    { DEFAULT: "#0d1117", 1: "#0a0f17", 2: "#161b22", 3: "#21262d" },
        line:  "#30363d",
        muted: "#8b949e",
        fg:    "#c9d1d9",
        accent:"#58a6ff",
        ok:    "#2dd4bf",
        warn:  "#f0c000",
        err:   "#f87171",
        plum:  "#b794f4",
      },
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Roboto', 'sans-serif'],
        mono: ['"SF Mono"', 'Menlo', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
}
