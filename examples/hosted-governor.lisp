;; hosted-governor.lisp — Manifest-bound worker and replay proof

(begin
  (if (> isru_alloc 0.80)
    (begin
      (set! heating_alloc 0.15)
      (set! isru_alloc 0.75)
      (set! greenhouse_alloc 0.10)
      (set! food_ration 0.60))
    (begin
      (set! heating_alloc 0.10)
      (set! isru_alloc 0.85)
      (set! greenhouse_alloc 0.05)
      (set! food_ration 0.50)))
  (rb-post
    "code"
    (concat "Governor decision for Sol " (string sol))
    "Dry-run effect: O2 emergency allocation proposed.")
  (concat "Governor: I=" (string isru_alloc)
          " G=" (string greenhouse_alloc)
          " H=" (string heating_alloc)))
