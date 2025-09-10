/** @type {import('tailwindcss').Config} */
export default {
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      fontFamily: {
        fredoka: ['Fredoka One', 'cursive'],
        caveat: ['Caveat', 'cursive'],
        comfortaa: ['Comfortaa', 'cursive'],
        righteous: ['Righteous', 'cursive'],
        bungee: ['Bungee', 'cursive'],
        creepster: ['Creepster', 'cursive'],
        monoton: ['Monoton', 'cursive'],
        faster: ['Faster One', 'cursive'],
        orbitron: ['Orbitron', 'cursive'],
      },
    },
  },
  plugins: [],
}
