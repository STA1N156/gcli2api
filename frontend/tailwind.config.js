/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        gray: { 400: "#a5adbf", 500: "#8992a8" },
        dark: {
          50: "#f8fafc",
          100: "#f1f5f9",
          200: "#e2e8f0",
          300: "#cbd5e1",
          400: "#94a3b8",
          500: "#64748b",
          600: "#41485c",
          700: "#2a2f3e",
          800: "#1c2130",
          900: "#151923",
          950: "#10131c",
        },
      },
    },
  },
  plugins: [],
};
