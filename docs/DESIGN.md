# Frontend Design System

## 1. Purpose and Scope

This document defines the visual design system for the AI Document Analyser frontend.

The goal is to establish a modern, technical, premium visual identity inspired by Vercel, using shadcn/ui, Radix UI, and the brand color `#B0E10A`.

**This is a visual-system migration, not a structural redesign.**

The existing layouts, page structures, component hierarchy, routes, component APIs, application logic, and behavior must remain unchanged.

The migration may change:

- Colors and semantic design tokens
- Typography and font styling
- Spacing and sizing
- Borders and radii
- Shadows and surface treatments
- Component variants and visual states
- Transitions and animations
- Light and dark theme styling

The migration must not change:

- Page layouts or component hierarchy
- Component responsibilities or public APIs
- Routing and navigation behavior
- Authentication or authorization
- API calls, data fetching, or business logic
- React state, form validation, or event handling
- WebSocket behavior or document-processing logic

Do not refactor unrelated code or introduce new features during this migration.

---

## 2. Visual Direction

### Design Personality

The application should feel:

- Modern and technical
- Precise and intelligent
- Minimal but not empty
- Premium without unnecessary decoration
- Fast and responsive
- Consistent and intentional

The interface should feel like a serious developer-oriented AI product rather than a generic AI SaaS template.

### Design Inspiration

Use Vercel's visual language as the primary inspiration.

Take inspiration from:

- Monochromatic foundations
- Strong typographic hierarchy
- Subtle borders and surface contrast
- Restrained use of brand color
- Compact, information-dense layouts
- Clean geometry
- Consistent interaction states
- Purposeful, smooth animations

Do not copy Vercel's interface directly. Maintain the product's own identity through its lime-green brand accent and document-focused experience.

### Core Visual Principle

The interface should feel impressive because of:

**Typography + spacing + contrast + alignment + consistency + motion.**

It should not depend on gradients, excessive shadows, decorative illustrations, or oversized rounded cards.

### Avoid

- Generic AI dashboard aesthetics
- Purple or blue AI gradients
- Glassmorphism
- Glowing cards and neon effects
- Decorative gradient blobs
- Excessive rounded containers
- Oversized headings and unnecessary whitespace
- Every section being wrapped in a card
- Excessive badges and pill-shaped elements
- Decorative icons or emoji
- Unnecessary animation
- Arbitrary colors and inconsistent spacing

---

## 3. Brand Identity

### Primary Brand Color

`#B0E10A`

This is the product's signature accent.

Use it selectively for:

- Primary actions
- Active and selected states
- Keyboard focus indicators
- Progress indicators
- Important interactive elements
- Subtle brand highlights

Do not use bright green as the default background for large areas or as the color for every positive state.

The interface should remain predominantly monochromatic.

### Color Discipline

All components must use semantic design tokens.

Preferred:

- `bg-background`
- `text-foreground`
- `bg-card`
- `bg-primary`
- `text-primary-foreground`
- `bg-muted`
- `text-muted-foreground`
- `border-border`
- `ring-ring`

Avoid raw hexadecimal colors, arbitrary Tailwind palette colors, and one-off color definitions in feature components.

The only exception is a genuinely data-driven visualization or third-party integration that requires its own explicitly documented color mapping.

---

## 4. Semantic Design Tokens

The existing `frontend/src/app/globals.css` is the implementation source of truth.

Use the existing shadcn semantic variable names and Tailwind v4 `@theme inline` mappings. Update their values instead of introducing a competing token system.

The following hex values define the intended palette. Convert them to valid OKLCH values when implementing the existing OKLCH-based CSS system. Preserve the intended appearance and check contrast after conversion.

### Light Theme — `:root`

| Token                    | Value     | Purpose                        |
| ------------------------ | --------- | ------------------------------ |
| `--background`           | `#FAFAFA` | Application background         |
| `--foreground`           | `#111111` | Primary text                   |
| `--card`                 | `#FFFFFF` | Card surfaces                  |
| `--card-foreground`      | `#111111` | Text on cards                  |
| `--popover`              | `#FFFFFF` | Menus and floating surfaces    |
| `--popover-foreground`   | `#111111` | Floating-surface text          |
| `--primary`              | `#B0E10A` | Brand and primary actions      |
| `--primary-foreground`   | `#111111` | Text on primary surfaces       |
| `--secondary`            | `#F4F4F5` | Secondary surfaces             |
| `--secondary-foreground` | `#18181B` | Secondary text                 |
| `--muted`                | `#F4F4F5` | Muted surfaces                 |
| `--muted-foreground`     | `#71717A` | Secondary text and metadata    |
| `--accent`               | `#F1F5DC` | Subtle brand-tinted surface    |
| `--accent-foreground`    | `#18181B` | Text on accent surfaces        |
| `--destructive`          | `#DC2626` | Errors and destructive actions |
| `--border`               | `#E4E4E7` | Default borders                |
| `--input`                | `#E4E4E7` | Input borders                  |
| `--ring`                 | `#B0E10A` | Focus indicators               |

### Dark Theme — `.dark`

| Token                    | Value     | Purpose                          |
| ------------------------ | --------- | -------------------------------- |
| `--background`           | `#09090B` | Application background           |
| `--foreground`           | `#FAFAFA` | Primary text                     |
| `--card`                 | `#111113` | Card surfaces                    |
| `--card-foreground`      | `#FAFAFA` | Text on cards                    |
| `--popover`              | `#111113` | Menus and floating surfaces      |
| `--popover-foreground`   | `#FAFAFA` | Floating-surface text            |
| `--primary`              | `#B0E10A` | Brand and primary actions        |
| `--primary-foreground`   | `#09090B` | Text on primary surfaces         |
| `--secondary`            | `#18181B` | Secondary surfaces               |
| `--secondary-foreground` | `#F4F4F5` | Secondary text                   |
| `--muted`                | `#18181B` | Muted surfaces                   |
| `--muted-foreground`     | `#A1A1AA` | Secondary text and metadata      |
| `--accent`               | `#1A2110` | Subtle dark brand-tinted surface |
| `--accent-foreground`    | `#D9F58A` | Text on accent surfaces          |
| `--destructive`          | `#EF4444` | Errors and destructive actions   |
| `--border`               | `#27272A` | Default borders                  |
| `--input`                | `#27272A` | Input borders                    |
| `--ring`                 | `#B0E10A` | Focus indicators                 |

### Token Implementation Requirements

- Keep the light and dark palettes independent.
- Preserve all existing shadcn-required variables.
- Retain `--chart-1` through `--chart-5`.
- Retain all existing sidebar tokens.
- Retain the existing Tailwind v4 `@theme inline` mapping.
- Keep foreground colors readable against their corresponding surfaces.
- Validate contrast for normal text, buttons, badges, focus indicators, and muted text.
- Do not blindly assign the brand color to all active or success states.

The bright lime accent may require dark text to achieve readable primary buttons. Do not assume the accent itself is suitable for small text on a white background.

### Activity-Feed Palette (Documented Exception)

The document activity log (per-doc panel inside the detail dialog, and the cross-doc feed on the documents page) is a data-driven visualization: each row is a pipeline-stage event from the backend. Eight event categories are distinguishable — **upload, download, parse, chunk, embed, ready, failed, info** — and showing the same neutral icon for every event makes the log visually flat.

To make the pipeline readable at a glance without breaking the monochromatic + lime rule, the activity feed is the **only** surface in the application that uses a per-category color. Each category gets a paired `--log-{category}-surface` (low-chroma tinted square behind the icon) and `--log-{category}-foreground` (icon color) token, both derived from a hue spaced around the color wheel:

| Category  | Hue anchor | Use                                                    |
| --------- | ---------- | ------------------------------------------------------ |
| upload    | blue 240°  | `upload.received`, `upload.confirmed`, `uploaded`      |
| download  | cyan 200°  | `download.started`, `download.completed`               |
| parse     | purple 290°| `parse.started`, `parse.completed`, `processing`       |
| chunk     | pink 330°  | `chunking.started`, `chunking.progress`, `chunking.*`  |
| embed     | green 150° | `embedding.*`, `learning`                              |
| ready     | green 155° | `ready` (lifecycle success)                            |
| failed    | red 25°    | `failed`, `document.deleted`                           |
| info      | neutral    | Fallback for future / unknown event types              |

Implementation rules for the activity-feed palette (this is the only allowed exception to the monochromatic rule, and only because the colors are tied 1:1 to a backend event category):

- Use **only** the `--log-{category}-surface` and `--log-{category}-foreground` tokens. Do not introduce per-event or per-document colors.
- Do **not** reuse these tokens outside the activity log — they are not general UI accents.
- Light theme: surfaces at ~95% lightness, foregrounds at ~45% lightness. Dark theme: surfaces at ~22% lightness, foregrounds at ~78% lightness. Same hue + chroma in both themes so the category identity is consistent.
- The `ready` and `embed` colors sit close to the brand lime. That is intentional — `ready` is the headline success signal and `embed` is the green/learning track. Do not re-tint them to "neutralize" them; the contrast against other categories is the point.
- Failed uses destructive-style red, paired with the existing `text-destructive` semantic token where a row-level error is needed outside the activity log.

---

## 5. Sidebar Tokens

The sidebar should feel integrated into the application shell, not like a floating card.

### Light Theme

| Token                          | Value     |
| ------------------------------ | --------- |
| `--sidebar`                    | `#F7F7F7` |
| `--sidebar-foreground`         | `#18181B` |
| `--sidebar-primary`            | `#B0E10A` |
| `--sidebar-primary-foreground` | `#111111` |
| `--sidebar-accent`             | `#EEEEEF` |
| `--sidebar-accent-foreground`  | `#18181B` |
| `--sidebar-border`             | `#E4E4E7` |
| `--sidebar-ring`               | `#B0E10A` |

### Dark Theme

| Token                          | Value     |
| ------------------------------ | --------- |
| `--sidebar`                    | `#0C0C0E` |
| `--sidebar-foreground`         | `#F4F4F5` |
| `--sidebar-primary`            | `#B0E10A` |
| `--sidebar-primary-foreground` | `#09090B` |
| `--sidebar-accent`             | `#18181B` |
| `--sidebar-accent-foreground`  | `#FAFAFA` |
| `--sidebar-border`             | `#27272A` |
| `--sidebar-ring`               | `#B0E10A` |

Use restrained active navigation backgrounds, consistent icon alignment, and clear text hierarchy. Do not make every navigation item bright green.

---

## 6. Typography

### Font

Use the existing Geist font configuration.

Do not introduce additional font families.

### Hierarchy

Use typography to establish importance instead of relying on decorative containers.

| Role             | Suggested Size | Style                    |
| ---------------- | -------------- | ------------------------ |
| Page title       | 24–30px        | Semibold, tight tracking |
| Section title    | 18–20px        | Semibold                 |
| Subsection title | 14–16px        | Medium or semibold       |
| Body             | 14–16px        | Regular                  |
| Controls         | 13–14px        | Medium where appropriate |
| Labels           | 12–13px        | Medium                   |
| Metadata         | 12–13px        | Regular, muted           |
| Fine print       | 11–12px        | Use sparingly            |

These are guidelines, not permission to resize every existing element indiscriminately.

Preserve existing content hierarchy and component dimensions unless a visual inconsistency requires adjustment.

### Typography Rules

- Use consistent heading weights.
- Avoid unnecessarily large headings.
- Keep body text readable.
- Use muted foreground tokens for secondary information.
- Use consistent line heights.
- Avoid excessive uppercase labels and letter spacing.
- Do not use font size alone to create hierarchy; combine size, weight, and contrast.

---

## 7. Spacing and Layout

Use a consistent spacing vocabulary.

Preferred spacing values in pixels:

`4, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96`

Use the closest appropriate value rather than introducing arbitrary spacing.

### Guidelines

- 4–8px for tightly related elements.
- 12–16px for controls and compact groups.
- 20–24px for component sections.
- 32–48px for major content groups.
- 64px and above for large page-level separation when appropriate.

These are guidelines, not a mandate to replace every existing margin or padding value.

Preserve the existing layout structure, content order, column arrangements, and responsive behavior.

Adjust spacing only where it improves consistency without changing the intended composition.

Do not artificially increase whitespace to make the interface look premium.

---

## 8. Radius, Borders, and Shadows

### Radius

Use a restrained radius system.

Set the base radius to:

`--radius: 0.5rem`

Recommended scale:

| Token       | Value      |
| ----------- | ---------- |
| Small       | `0.375rem` |
| Medium      | `0.5rem`   |
| Large       | `0.625rem` |
| Extra large | `0.75rem`  |

Preserve existing shadcn radius mappings and component compatibility.

Use larger radii only where the component genuinely benefits from them.

Avoid excessive rounding on cards, buttons, dialogs, and page containers.

### Borders

Borders should be subtle and consistent.

- Use `border-border` for ordinary separators.
- Use `border-input` for input boundaries.
- Use `ring-ring` for focus indicators.
- Use stronger visual emphasis only for selection, focus, or meaningful state changes.

Prefer borders and whitespace over shadows for ordinary content separation.

### Shadows

Keep ordinary surfaces mostly flat.

Use restrained shadows for elevated elements such as:

- Dialogs
- Dropdown menus
- Popovers
- Command palettes
- Floating panels

Do not add shadows to every card or section.

---

## 9. Component Styling Rules

The existing shadcn components under `frontend/src/components/ui/` remain the foundational primitives.

All feature components should compose these primitives rather than reimplementing their behavior or styling conventions.

### Buttons

Use the existing Button component and its variants.

**Primary**

- Brand-green background.
- Dark foreground.
- Clear hover and focus states.
- Used for the primary action in a region.

**Secondary**

- Neutral background and foreground.
- Used for supporting actions.

**Outline**

- Subtle border.
- Neutral background.
- Used for lower-emphasis actions that still need a visible boundary.

**Ghost**

- Minimal default styling.
- Subtle hover background.
- Used for navigation controls and low-emphasis actions.

**Destructive**

- Reserved for destructive operations.
- Use the destructive semantic token.

Do not create page-specific button designs when an existing variant can be reused.

### Inputs and Textareas

Use consistent:

- Height
- Padding
- Radius
- Border
- Background
- Placeholder styling
- Focus ring
- Disabled appearance
- Error appearance

Use semantic tokens rather than raw colors.

Focus should be clear and accessible without excessive glow.

### Cards

Cards are not the default layout primitive.

Use them when content needs a distinct boundary or grouping.

Prefer whitespace and borders where they communicate the hierarchy adequately.

Keep card backgrounds, borders, and radii consistent between themes.

Do not wrap every section in a separate card.

### Badges

Badges communicate status, metadata, or categorization.

Keep them compact and consistent.

Do not use badges as decoration.

Do not use the primary lime color for every status.

### Navigation

Navigation should feel compact, clear, and intentional.

- Active items should be distinguishable.
- Icons should have consistent dimensions.
- Hover states should be subtle.
- Selected items should not overpower the main content.
- Use sidebar semantic tokens.

### Dialogs and Dropdowns

Use the existing shadcn/Radix primitives.

Apply consistent surface colors, borders, radii, and restrained shadows.

Animations should be short and smooth.

Preserve focus management, keyboard interaction, and accessibility.

### Icons

Use Lucide consistently, following the existing setup.

Use consistent icon sizing and stroke appearance.

Icons should support meaning and navigation, not serve as decoration.

### Loading, Empty, and Error States

Preserve existing state behavior while ensuring consistent visual treatment.

- Loading: use existing indicators or skeletons where appropriate.
- Empty: explain the state clearly and preserve existing actions.
- Error: use destructive semantic tokens without overwhelming the entire page.
- Disabled: reduce emphasis while maintaining legibility.
- Processing: distinguish ongoing work from successful completion.
- Success: use a consistent semantic treatment that is separate from the brand accent when necessary.

Do not invent new business states or change how the application transitions between them.

---

## 10. Animation and Motion

The application should feel smooth and responsive, with enough motion to feel polished but not distracting.

### Motion Durations

| Token  | Duration | Usage                                   |
| ------ | -------- | --------------------------------------- |
| Fast   | `150ms`  | Hover and small control transitions     |
| Normal | `200ms`  | Buttons and standard interactions       |
| Medium | `250ms`  | Popovers and subtle content transitions |
| Slow   | `350ms`  | Larger layout transitions, when needed  |

Most interactions should use 150–250ms.

### Motion Style

Prefer subtle transitions involving:

- Opacity
- Transform
- Background color
- Border color
- Shadow
- Existing Radix open/close states

Use smooth, consistent easing.

Avoid excessive bouncing, dramatic scaling, large entrance animations, and decorative movement.

Do not delay interactions until an animation finishes.

### Where Motion Is Appropriate

- Button hover and active states
- Navigation interactions
- Dialogs and dropdowns
- Popovers
- Existing sidebar transitions
- Loading and processing indicators
- Existing status changes
- Theme transitions
- Existing content expansion and collapse

Do not add animations that alter application logic or change the layout unnecessarily.

Respect `prefers-reduced-motion` and disable or minimize non-essential motion when requested by the user's operating system.

Do not add a new animation library unless it is genuinely required by the existing implementation.

---

## 11. Light and Dark Themes

Both themes are first-class visual experiences.

Dark mode must not be implemented as a collection of component-specific overrides.

Use the same semantic token names in both themes and change their values in the token layer.

Prefer:

- `bg-background`
- `bg-card`
- `text-foreground`
- `text-muted-foreground`
- `border-border`
- `bg-primary`

Avoid unnecessary classes such as:

- `bg-white dark:bg-zinc-900`
- `text-black dark:text-white`

Use component-level theme overrides only when a real design requirement cannot be represented by existing semantic tokens.

The existing `.dark` variables can be retained, but the ability to switch themes must be verified separately.

If theme switching is not currently wired up, treat its implementation as a separate, explicitly approved task. Do not silently introduce unrelated dependencies or alter the application's theme behavior during the token migration.

---

## 12. Implementation Architecture

The design system should follow this existing hierarchy:

`globals.css semantic tokens → shadcn/ui primitives → feature components → existing pages`

### Source of Truth

- `frontend/components.json`: preserve the existing shadcn configuration.
- `frontend/src/app/globals.css`: source of truth for semantic color and theme tokens.
- `frontend/src/components/ui/`: foundational shadcn primitives.
- Existing feature components: consume semantic tokens and shared primitives.
- This `design.md`: source of truth for visual intent and styling rules.

Keep the existing `radix-vega` configuration, neutral foundation, Lucide icons, CSS variable support, aliases, and Tailwind v4 setup.

Do not create a second design-token framework.

Do not duplicate semantic colors across multiple files unnecessarily.

---

## 13. Visual Migration Rules for AI Coding Agents

Before modifying code:

1. Inspect the existing design tokens.
2. Inspect existing shadcn primitives.
3. Inspect feature-component styling.
4. Identify hard-coded colors and inconsistent visual values.
5. Separate visual issues from functional issues.
6. Make a scoped migration plan.

Implement the migration incrementally.

Recommended order:

1. Update semantic tokens in `globals.css`.
2. Verify light and dark palette values.
3. Update existing shadcn primitive styling.
4. Replace conflicting hard-coded feature-component styles.
5. Audit spacing, radius, borders, and typography.
6. Review existing interaction and responsive states.
7. Add or refine subtle motion.
8. Perform a final visual consistency audit.

Do not redesign every page at once.

Do not rewrite whole components just to change their visual appearance.

Prefer the smallest change that achieves visual consistency.

### Prohibited During This Migration

- Rebuilding pages or layouts
- Replacing existing component architecture
- Changing component APIs
- Refactoring business logic
- Changing API requests or response handling
- Changing state management
- Modifying authentication or routing
- Changing WebSocket or document-processing behavior
- Adding unnecessary dependencies
- Creating a parallel component library
- Replacing working functionality with mock data

If a requested visual improvement genuinely requires structural changes, explain the limitation and ask for approval before making those changes.

---

## 14. Definition of Done

The visual migration is complete when:

- [ ] Light and dark themes use the defined semantic palette.
- [ ] The brand color is consistently represented by the primary token.
- [ ] Existing shadcn primitives follow the visual direction.
- [ ] Feature components primarily use semantic tokens.
- [ ] Unnecessary hard-coded colors have been removed.
- [ ] Typography is consistent.
- [ ] Spacing and radii follow the design vocabulary.
- [ ] Borders and shadows are restrained and consistent.
- [ ] Buttons, inputs, dialogs, dropdowns, and badges have consistent styling.
- [ ] Existing loading, empty, error, disabled, and processing states remain functional.
- [ ] Motion is consistent and respects reduced-motion preferences.
- [ ] Both themes are visually checked.
- [ ] Responsive layouts continue to work.
- [ ] Existing functionality and component behavior are preserved.
- [ ] No unrelated refactoring or feature changes were introduced.

The final result should feel like a cohesive, modern, Vercel-inspired technical application with its own lime-green identity.

**The goal is not to make the UI more complicated. The goal is to make the existing UI more intentional, consistent, and polished.**
