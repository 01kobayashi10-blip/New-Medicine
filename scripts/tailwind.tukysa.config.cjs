/** @type {import('tailwindcss').Config} */
module.exports = {
  // cwd はリポジトリルート想定（content はプロジェクトルート基準）
  content: ["./reports/infographic_tukysa.html"],
  theme: {
    extend: {
      colors: {
        brand: { dark: "#4c1d95", mid: "#7c3aed", light: "#ede9fe" },
      },
      fontFamily: {
        sans: [
          '"Hiragino Sans"',
          '"Hiragino Kaku Gothic ProN"',
          "Meiryo",
          "sans-serif",
        ],
      },
    },
  },
  plugins: [],
};
