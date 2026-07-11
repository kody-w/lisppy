;; gauntlet-runner.lisp — External gauntlet score preview
;;
;; This computes a single-state projection. It does not invoke or prove the
;; external Monte Carlo gauntlet.
;;
;; Usage in vOS terminal:
;;   (load "examples/mars-barn/gauntlet-runner.lisp")

(begin
  (display "=== Mars Barn Gauntlet Runner ===")
  (newline)

  ;; Check colony status
  (display (string-append "Current Sol: " (number->string sol)))
  (newline)
  (display (string-append "CRI: " (number->string colony_risk_index)))
  (newline)
  (display (string-append "Crew: " (number->string crew_alive) "/" (number->string crew_total)))
  (newline)
  (display (string-append "Modules: " (number->string modules_built)))
  (newline)

  ;; The scoring formula (Amendment IV)
  ;; SCORE = median_sols × 100
  ;;       + min_crew × 500
  ;;       + min(modules, 8) × 150
  ;;       + survival_rate × 20000
  ;;       - P75_CRI × 10

  (define projected-score
    (+ (* sol 100)
       (* crew_alive 500)
       (* (min modules_built 8) 150)
       20000
       (* colony_risk_index -10)))

  (display (string-append "Projected score: " (number->string projected-score)))
  (newline)

  (define grade
    (cond
      ((>= projected-score 90000) "S+")
      ((>= projected-score 70000) "S")
      ((>= projected-score 50000) "A")
      ((>= projected-score 30000) "B")
      ((>= projected-score 15000) "C")
      (#t "D")))

  (display (string-append "Grade: " grade))
  (newline)

  (display "Run official gauntlet: node tools/gauntlet.js --monte-carlo 100")
  (newline))
