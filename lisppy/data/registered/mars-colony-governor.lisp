;; mars-colony-governor.lisp — Colony resource allocation governor
;;
;; Locally executed as a Python-hosted governor candidate. Compatibility with
;; the external Mars Barn viewer dialect is not established in this repository.
;;
;; This is the same pattern as data sloshing:
;;   Read state → Eval governor → Print allocations → Loop (next sol)
;;
;; Local proof: tests/test_hosted_governor.py

(begin
  ;; Assess — read the world (env vars seeded by sim)
  (define o2-critical (< o2_days 5))
  (define h2o-critical (< h2o_days 5))
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

    ;; Water emergency: ISRU also supplies life-support water
    (h2o-critical
      (begin
        (set! isru_alloc 0.80)
        (set! greenhouse_alloc 0.08)
        (set! heating_alloc 0.12)
        (set! food_ration 0.55)
        (display "💧 H₂O EMERGENCY — ISRU maximized")
        (newline)))

    ;; Food running low: greenhouse priority
    (food-critical
      (begin
        (set! isru_alloc 0.25)
        (set! greenhouse_alloc 0.60)
        (set! heating_alloc 0.15)
        (set! food_ration 0.65)
        (display "🌱 Food priority — greenhouse boosted")
        (newline)))

    ;; Power critical: heating to keep systems alive
    (power-critical
      (begin
        (set! heating_alloc 0.55)
        (set! isru_alloc 0.25)
        (set! greenhouse_alloc 0.20)
        (set! food_ration 0.75)
        (display "⚡ Power critical — heating priority")
        (newline)))

    ;; High risk: defensive posture
    (high-risk
      (begin
        (set! heating_alloc 0.40)
        (set! isru_alloc 0.35)
        (set! greenhouse_alloc 0.25)
        (set! food_ration 0.80)
        (display "🛡️ High CRI — defensive allocation")
        (newline)))

    ;; Nominal: balanced growth
    (#t
      (begin
        (set! isru_alloc 0.35)
        (set! greenhouse_alloc 0.40)
        (set! heating_alloc 0.25)
        (set! food_ration 1.0)
        (display "✓ Nominal — balanced growth")
        (newline))))

  ;; Return summary (the output of this frame)
  (string-append "Governor: I=" (number->string isru_alloc)
    " G=" (number->string greenhouse_alloc)
    " H=" (number->string heating_alloc)))
