;; mars-colony-governor.lisp — Colony resource allocation governor
;;
;; Runs inside the Mars Barn sim (https://github.com/kody-w/mars-barn-opus).
;; The VM seeds environment variables before this program runs.
;; The program reads colony state, makes allocation decisions, writes them back.
;;
;; This is the same pattern as data sloshing:
;;   Read state → Eval governor → Print allocations → Loop (next sol)
;;
;; Usage in Mars Barn:
;;   Paste into viewer.html governor editor, or
;;   Load via tools/vos-headless.js --script this-file.lisp

(begin
  ;; Assess — read the world (env vars seeded by sim)
  (define o2-critical (< o2_days 5))
  (define food-critical (< food_days 10))
  (define power-critical (< power_kwh 80))
  (define high-risk (> colony_risk_index 50))

  ;; Log the assessment
  (display (string-append "Sol " (number->string sol)
    " | CRI:" (number->string colony_risk_index)
    " | O₂:" (number->string o2_days) "d"
    " | Food:" (number->string food_days) "d"
    " | Power:" (number->string power_kwh) "kWh"))
  (newline)

  ;; Decide — cond branches are the governor's brain
  (cond
    ;; O₂ emergency: all power to ISRU
    (o2-critical
      (begin
        (set! isru_alloc 0.85)
        (set! greenhouse_alloc 0.05)
        (set! heating_alloc 0.10)
        (set! food_ration 0.5)
        (display "⚠️ O₂ EMERGENCY — ISRU maximized")
        (newline)))

    ;; Food running low: greenhouse priority
    (food-critical
      (begin
        (set! isru_alloc 0.25)
        (set! greenhouse_alloc 0.60)
        (set! heating_alloc 0.15)
        (display "🌱 Food priority — greenhouse boosted")
        (newline)))

    ;; Power critical: heating to keep systems alive
    (power-critical
      (begin
        (set! heating_alloc 0.55)
        (set! isru_alloc 0.25)
        (set! greenhouse_alloc 0.20)
        (display "⚡ Power critical — heating priority")
        (newline)))

    ;; High risk: defensive posture
    (high-risk
      (begin
        (set! heating_alloc 0.40)
        (set! isru_alloc 0.35)
        (set! greenhouse_alloc 0.25)
        (display "🛡️ High CRI — defensive allocation")
        (newline)))

    ;; Nominal: balanced growth
    (#t
      (begin
        (set! isru_alloc 0.35)
        (set! greenhouse_alloc 0.40)
        (set! heating_alloc 0.25)
        (display "✓ Nominal — balanced growth")
        (newline))))

  ;; Return summary (the output of this frame)
  (string-append "Governor: I=" (number->string isru_alloc)
    " G=" (number->string greenhouse_alloc)
    " H=" (number->string heating_alloc)))
