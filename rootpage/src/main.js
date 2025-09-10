document.addEventListener('DOMContentLoaded', () => {
  const words = document.querySelectorAll('.word')

  // Click effects for each word
  words.forEach((word, index) => {
    word.addEventListener('click', (e) => {
      // Create explosion effect
      createWordExplosion(e.pageX, e.pageY, index)

      // Shake the entire page briefly
      document.body.style.animation = 'pageShake 0.5s ease-in-out'
      setTimeout(() => {
        document.body.style.animation = ''
      }, 500)
    })

    // Add special hover sounds (visual representation)
    word.addEventListener('mouseenter', () => {
      createHoverSparkle(word)
    })
  })

  // Create word explosion
  function createWordExplosion(x, y, wordIndex) {
    const explosionEmojis = [
      ['💕', '✨', '🌸'], // Word 1: hearts and flowers
      ['💜', '🌀', '👑'], // Word 2: purple tornado
      ['💙', '⚡', '🔥'], // Word 3: blue electric
      ['🌈', '🎉', '🎊'], // Word 4: rainbow chaos
      ['💚', '🤖', '⚡'], // Word 5: matrix green
      ['🧡', '🔥', '💥'], // Word 6: fire orange
      ['🩵', '🌊', '💎'], // Word 7: cyber teal
      ['🌟', '💖', '🦄', '🌈', '✨'], // Word 8: ultimate explosion
    ]

    const emojis = explosionEmojis[wordIndex] || ['✨', '💕', '🌈']

    for (let i = 0; i < 20; i++) {
      setTimeout(() => {
        const explosion = document.createElement('div')
        explosion.innerHTML = emojis[Math.floor(Math.random() * emojis.length)]
        explosion.style.position = 'fixed'
        explosion.style.left = `${x}px`
        explosion.style.top = `${y}px`
        explosion.style.fontSize = '2rem'
        explosion.style.pointerEvents = 'none'
        explosion.style.zIndex = '10000'
        explosion.style.animation = `wordExplosion ${1 + Math.random()}s ease-out forwards`

        document.body.appendChild(explosion)

        setTimeout(() => {
          explosion.remove()
        }, 2000)
      }, i * 50)
    }
  }

  // Create hover sparkle
  function createHoverSparkle(element) {
    const rect = element.getBoundingClientRect()
    const sparkle = document.createElement('div')
    sparkle.innerHTML = '✨'
    sparkle.style.position = 'fixed'
    sparkle.style.left = `${rect.left + rect.width / 2}px`
    sparkle.style.top = `${rect.top + rect.height / 2}px`
    sparkle.style.fontSize = '1.5rem'
    sparkle.style.pointerEvents = 'none'
    sparkle.style.zIndex = '9999'
    sparkle.style.animation = 'hoverSparkle 0.8s ease-out forwards'

    document.body.appendChild(sparkle)

    setTimeout(() => {
      sparkle.remove()
    }, 800)
  }

  // Random emoji storms every 15 seconds
  setInterval(() => {
    createEmojiStorm()
  }, 15000)

  function createEmojiStorm() {
    const stormEmojis = ['💕', '✨', '🌸', '🦄', '🌈', '💖', '🦋', '⭐', '💫', '🍭', '🎊', '🎉', '🌺']

    for (let i = 0; i < 30; i++) {
      setTimeout(() => {
        const storm = document.createElement('div')
        storm.innerHTML = stormEmojis[Math.floor(Math.random() * stormEmojis.length)]
        storm.style.position = 'fixed'
        storm.style.left = `${Math.random() * 100}%`
        storm.style.top = '-50px'
        storm.style.fontSize = `${1 + Math.random() * 2}rem`
        storm.style.pointerEvents = 'none'
        storm.style.zIndex = '9998'
        storm.style.animation = `emojiStorm ${3 + Math.random() * 2}s linear forwards`

        document.body.appendChild(storm)

        setTimeout(() => {
          storm.remove()
        }, 5000)
      }, i * 100)
    }
  }

  // Konami code for MEGA BESTIES MODE
  let konamiCode = []
  const konamiSequence = [
    'ArrowUp',
    'ArrowUp',
    'ArrowDown',
    'ArrowDown',
    'ArrowLeft',
    'ArrowRight',
    'ArrowLeft',
    'ArrowRight',
    'KeyB',
    'KeyA',
  ]

  document.addEventListener('keydown', (e) => {
    konamiCode.push(e.code)

    if (konamiCode.length > konamiSequence.length) {
      konamiCode.shift()
    }

    if (konamiCode.join(',') === konamiSequence.join(',')) {
      activateMegaBestiesMode()
      konamiCode = []
    }
  })

  function activateMegaBestiesMode() {
    // ULTIMATE CHAOS MODE
    document.body.style.animation = 'megaRainbow 0.2s ease-in-out infinite'

    // Create massive emoji explosion
    for (let i = 0; i < 100; i++) {
      setTimeout(() => {
        const mega = document.createElement('div')
        mega.innerHTML = ['🦄', '✨', '🌈', '💖', '🌟', '💕'][Math.floor(Math.random() * 6)]
        mega.style.position = 'fixed'
        mega.style.left = `${Math.random() * 100}%`
        mega.style.top = `${Math.random() * 100}%`
        mega.style.fontSize = `${2 + Math.random() * 4}rem`
        mega.style.pointerEvents = 'none'
        mega.style.zIndex = '10001'
        mega.style.animation = 'megaExplosion 3s ease-out forwards'

        document.body.appendChild(mega)

        setTimeout(() => {
          mega.remove()
        }, 3000)
      }, i * 30)
    }

    // Reset after 5 seconds
    setTimeout(() => {
      document.body.style.animation = ''
    }, 5000)
  }

  // Add all the animation CSS
  const style = document.createElement('style')
  style.textContent = `
    @keyframes pageShake {
      0%, 100% { transform: translateX(0); }
      25% { transform: translateX(-2px); }
      75% { transform: translateX(2px); }
    }

    @keyframes wordExplosion {
      0% {
        transform: scale(0) rotate(0deg);
        opacity: 1;
      }
      100% {
        transform: scale(2) rotate(${Math.random() * 720 - 360}deg)
                   translate(${Math.random() * 200 - 100}px, ${Math.random() * 200 - 100}px);
        opacity: 0;
      }
    }

    @keyframes hoverSparkle {
      0% {
        transform: scale(0) rotate(0deg);
        opacity: 1;
      }
      50% {
        transform: scale(1.5) rotate(180deg);
        opacity: 1;
      }
      100% {
        transform: scale(0) rotate(360deg);
        opacity: 0;
      }
    }

    @keyframes emojiStorm {
      0% {
        transform: translateY(-50px) rotate(0deg);
        opacity: 0;
      }
      10% {
        opacity: 1;
      }
      90% {
        opacity: 1;
      }
      100% {
        transform: translateY(100vh) rotate(720deg);
        opacity: 0;
      }
    }

    @keyframes megaRainbow {
      0% { filter: hue-rotate(0deg) saturate(1); }
      25% { filter: hue-rotate(90deg) saturate(1.5); }
      50% { filter: hue-rotate(180deg) saturate(2); }
      75% { filter: hue-rotate(270deg) saturate(1.5); }
      100% { filter: hue-rotate(360deg) saturate(1); }
    }

    @keyframes megaExplosion {
      0% {
        transform: scale(0) rotate(0deg);
        opacity: 1;
      }
      50% {
        transform: scale(3) rotate(360deg);
        opacity: 1;
      }
      100% {
        transform: scale(6) rotate(720deg);
        opacity: 0;
      }
    }
  `
  document.head.appendChild(style)

  // Create initial welcome explosion
  setTimeout(() => {
    createEmojiStorm()
  }, 1000)
})
