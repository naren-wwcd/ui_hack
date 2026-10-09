/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#080d17",
        panel: "#111a28",
        line: "#263448",
        cyan: "#4dd9e8",
        mint: "#47d7a0",
        danger: "#ff6278",
        amber: "#ffbd59",
      },
      boxShadow: {
        glow: "0 0 28px rgba(77, 217, 232, 0.10)",
      },
    },
  },
  plugins: [],
};