/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // TrueNAS-ish dark, purple accent
        bg: "#0e1116",
        panel: "#171a21",
        border: "#252a33",
        muted: "#8b95a6",
        accent: "#7c3aed",         // violet-600
        "accent-hover": "#8b5cf6", // violet-500
        success: "#3ddc84",
        danger: "#ff5252",
        warning: "#ffb74d",
      },
    },
  },
  plugins: [],
};
