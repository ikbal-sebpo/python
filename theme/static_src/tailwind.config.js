/**
 * Tailwind config for Orbis — Timely brand tokens.
 * See docs/PLAN.md → "Tailwind + Timely brand tokens".
 */

module.exports = {
    darkMode: 'class',
    content: [
        '../templates/**/*.html',
        '../../templates/**/*.html',
        '../../**/templates/**/*.html',
    ],
    theme: {
        extend: {
            fontFamily: {
                sans: ['Figtree', 'Roboto', 'ui-sans-serif', 'system-ui', 'sans-serif'],
                body: ['Roboto', 'Figtree', 'ui-sans-serif', 'system-ui', 'sans-serif'],
            },
            colors: {
                brand: {
                    DEFAULT: '#0A4958',
                    light:   '#0F6070',
                    accent:  '#41B3B3',
                    navy:    '#0B333F',
                    blue:    '#006F99',
                    teal:    '#57B6B2',
                    gold:    '#FFC638',
                },
                bg: {
                    main: '#F5F7F8',
                    card: '#ffffff',
                    soft: '#ECEFF1',
                },
                text: {
                    main: '#0A1A1A',
                    soft: '#4B5C6B',
                    mute: '#8A9AA6',
                },
            },
            borderRadius: {
                DEFAULT: '0.5rem',
            },
            boxShadow: {
                card: '0 1px 2px rgba(10, 26, 26, 0.04), 0 1px 3px rgba(10, 26, 26, 0.06)',
            },
        },
    },
    plugins: [
        require('@tailwindcss/forms'),
        require('@tailwindcss/typography'),
        require('@tailwindcss/aspect-ratio'),
    ],
}
