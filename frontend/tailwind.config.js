/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // TrueNAS-ish dark
        bg: "#0e1116",
        panel: "#171a21",
        border: "#252a33",
        muted: "#8b95a6",
        accent: "#2196f3",
        success: "#3ddc84",
        danger: "#ff5252",
        warning: "#ffb74d",
      },
    },
  },
  plugins: [],
};
