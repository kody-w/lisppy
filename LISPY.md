;; ═══════════════════════════════════════════════════════════════
;; LISPY ROSETTA STONE — Drop this file into any repo.
;; Any AI that reads it immediately understands the full language.
;; No docs needed. This IS the docs. Every feature, one file.
;; ═══════════════════════════════════════════════════════════════

;; ── 1. BASICS: Values and Variables ──

(define x 42)                     ;; bind a name to a value
(define name "Mars Colony")       ;; strings use double quotes
(define pi 3.14159)               ;; floats work
(define alive true)               ;; booleans: true, false

;; set! mutates an existing binding
(set! x 43)


;; ── 2. MATH ──

(+ 1 2 3)          ;; → 6        variadic addition
(- 10 3)            ;; → 7        subtraction
(* 2 3 4)           ;; → 24       variadic multiplication
(/ 10 3)            ;; → 3.333    division
(% 10 3)            ;; → 1        modulo (alias: mod)
(abs -5)            ;; → 5
(round 3.7)         ;; → 4
(floor 3.9)         ;; → 3
(ceil 3.1)          ;; → 4
(min 3 7 1 9)       ;; → 1
(max 3 7 1 9)       ;; → 9
(pow 2 10)          ;; → 1024
(sqrt 144)          ;; → 12
(sin 0)             ;; → 0
(cos 0)             ;; → 1
(pi)                ;; → 3.14159...
(random)            ;; → 0.0-1.0  pseudo-random


;; ── 3. COMPARISON + LOGIC ──

(< 3 5)             ;; → true
(> 3 5)             ;; → false
(<= 3 3)            ;; → true
(>= 5 5)            ;; → true
(= 42 42)           ;; → true     equality (works on strings too)
(!= "a" "b")        ;; → true
(and true true)      ;; → true    all must be true
(or false true)      ;; → true    any true
(not false)          ;; → true


;; ── 4. STRINGS ──

(concat "Hello" " " "Mars")       ;; → "Hello Mars"
(string 42)                        ;; → "42"        number to string
(number "42")                      ;; → 42          string to number
(length "Mars")                    ;; → 4


;; ── 5. CONTROL FLOW ──

;; if: (if condition then else)
(if (> x 10)
  "big"
  "small")

;; cond: multi-branch (like switch/case)
(cond
  ((< x 0)   "negative")
  ((= x 0)   "zero")
  ((< x 100) "small")
  (true       "big"))              ;; true = default case


;; ── 6. BLOCKS ──

;; begin: run multiple expressions, return the last one
(begin
  (define a 10)
  (define b 20)
  (+ a b))                         ;; → 30


;; ── 7. LISTS ──

(list 1 2 3 "four")               ;; → [1, 2, 3, "four"]
(nth (list "a" "b" "c") 1)        ;; → "b"         0-indexed
(length (list 1 2 3))             ;; → 3

;; map: transform each element (_ = current item)
(map (+ _ 1) (list 1 2 3))        ;; → [2, 3, 4]

;; filter: keep elements where predicate is true
(filter (> _ 2) (list 1 2 3 4))   ;; → [3, 4]

;; reduce: fold list into single value (_acc = accumulator, _ = item)
(reduce (+ _acc _) (list 1 2 3 4) 0)  ;; → 10

;; range: generate number sequences
(range 5)                          ;; → [0, 1, 2, 3, 4]
(range 2 6)                        ;; → [2, 3, 4, 5]


;; ── 8. LOOPS ──

;; repeat: run body N times (_i = iteration index, 0-based)
(repeat 5
  (log (concat "iteration " (string _i))))

;; let: temporary bindings (scoped, cleaned up after)
(let (x 100 y 200)
  (+ x y))                        ;; → 300, x/y restored after


;; ── 9. OUTPUT ──

(log "Hello from LisPy!")          ;; prints to output (variadic)
(log "x =" x "y =" 42)            ;; multiple args joined by space
(print "same as log")             ;; alias


;; ── 10. HTTP + JSON (async pre-fetch cache) ──

;; Runtime pre-fetches data before LisPy runs.
;; LisPy reads from cache synchronously.
(http-get "mars_weather")          ;; → cached JSON object or null
(json-get some_object "key")       ;; → extract key from object
(json-keys some_object)            ;; → list of keys


;; ── 11. PROMPT LIBRARY ──

;; Look up reusable programs from the built-in library
(prompt "governor-adaptive")               ;; → the program text
(prompt-list)                              ;; → all prompt names
(prompt-tags "economy")                    ;; → prompts tagged "economy"

;; With template variables:
(prompt "assess-o2" "threshold" 5)         ;; replaces {{threshold}} with 5


;; ── 12. ENVIRONMENT VARIABLES ──
;; The VM is seeded with context before your program runs.
;; Read them like any variable. Write allocation vars with set!

;; Read (always available):
;;   sol, o2_days, h2o_days, food_days, power_kwh
;;   crew_alive, crew_total, morale, colony_risk_index
;;   solar_eff, isru_eff, dust_tau, mars_temp_k
;;   modules_built, research_count, events_active

;; Write (your program's decisions):
;;   heating_alloc, isru_alloc, greenhouse_alloc, food_ration


;; ── 13. PUTTING IT ALL TOGETHER ──
;; A real governor program that keeps a colony alive:

(begin
  ;; Assess the situation
  (define critical (> colony_risk_index 50))
  (define o2_ok (> o2_days 10))
  (define power_ok (> power_kwh 100))
  (define food_ok (> food_days 15))

  ;; Log the assessment
  (log (concat "Sol " (string sol) " CRI:" (string colony_risk_index)))

  ;; Decide allocations based on what's most urgent
  (cond
    ;; O₂ emergency: all power to ISRU
    ((< o2_days 5)
      (begin
        (set! isru_alloc 0.85)
        (set! greenhouse_alloc 0.05)
        (set! heating_alloc 0.10)
        (set! food_ration 0.5)
        (log "⚠ O₂ EMERGENCY")))

    ;; Food running low: greenhouse priority
    ((< food_days 10)
      (begin
        (set! isru_alloc 0.25)
        (set! greenhouse_alloc 0.60)
        (set! heating_alloc 0.15)
        (log "🌱 Food priority")))

    ;; Power critical: heating to keep systems alive
    ((< power_kwh 80)
      (begin
        (set! heating_alloc 0.55)
        (set! isru_alloc 0.25)
        (set! greenhouse_alloc 0.20)
        (log "⚡ Power critical")))

    ;; All good: balanced growth
    (true
      (begin
        (set! isru_alloc 0.35)
        (set! greenhouse_alloc 0.40)
        (set! heating_alloc 0.25)
        (log "✓ Nominal — balanced growth"))))

  ;; Return a summary
  (concat "Governor decided: I=" (string isru_alloc)
          " G=" (string greenhouse_alloc)
          " H=" (string heating_alloc)))


;; ── 14. AGENT INTERFACE ──
;; Every .lispy agent follows this contract:

(begin
  ;; Identity (required)
  (define _agent_name "my_agent")
  (define _agent_desc "What this agent does")

  ;; Logic (the perform() body)
  ;; Read from env vars (pre-seeded by runtime)
  ;; Write results via set! or log
  ;; Return value = the agent's output

  ;; This IS the agent. Same as a Python class with perform(),
  ;; but without the class wrapper. The S-expression IS perform().
  (log (concat _agent_name " executed")))

;; The .lispy file IS the .py file's perform() body, unwrapped.
;; unRAPP converts between them automatically:
;;   basic_agent.py  ↔  basic_agent.lispy
;;   Same name. Same logic. Different runtime.
;;   The user carries whichever they prefer.


;; ── 15. CARTRIDGE BOOT ──
;; An OS or sim can boot from a .cartridge.json:

;; Boot sequence reads the cartridge's "boot.init" array
;; and executes each S-expression in order.
;; The cartridge's "env" field seeds the VM vars.
;; The cartridge's "filesystem" becomes virtual files.
;; (cat "/path") reads a file. (run "/bin/program") executes one.


;; ── 16. THE vOS SDK ──
;; In the browser OS (os.html), the SDK is on window.os:
;;   os.exec("(+ 2 3)")         — run this program
;;   os.open("terminal")        — open an app window
;;   os.type("terminal","code") — type + execute in terminal
;;   os.status()                — colony state object
;;   os.gauntlet(100)           — run Monte Carlo competition
;;   os.loadDistro("url")       — boot a cartridge
;;   os.fs()                    — list virtual files
;;   os.cat("/path")            — read a file
;;   os.run("/bin/prog")        — run a program
;;   os.batch([...])            — multiple commands (Playwright)
;;
;; Playwright drives the SDK: page.evaluate(() => window.os.exec("..."))
;; The console IS the API. The SDK IS the agent interface.


;; ═══════════════════════════════════════════════════════════════
;; QUICK REFERENCE
;; ═══════════════════════════════════════════════════════════════
;;
;; VALUES:   42  3.14  "string"  true  false
;; BIND:     (define name value)    (set! name new_value)
;; MATH:     + - * / % abs round floor ceil min max pow sqrt sin cos
;; COMPARE:  < > <= >= = !=
;; LOGIC:    and or not
;; STRINGS:  concat string number length
;; LISTS:    list nth length map filter reduce range
;; FLOW:     if cond begin let repeat
;; OUTPUT:   log print
;; DATA:     http-get json-get json-keys
;; PROMPTS:  prompt prompt-list prompt-tags
;; AGENTS:   _agent_name _agent_desc (define in every .lispy agent)
;; SDK:      os.exec os.open os.status os.gauntlet os.fs os.cat os.run
;; SPECIAL:  random pi
;;
;; HOMOICONIC: This program IS data. An AI can read it, modify it,
;; and generate new programs using the same syntax. The code is a
;; list of lists — the AST is the source code.
;;
;; Same VM runs in: browser (viewer.html), CLI (node/python),
;; physical hardware (Rappter), LisPy OS (os.html), and inside
;; the sim (agents). A program written anywhere runs everywhere. 1:1.
;;
;; unRAPP: .py ↔ .lispy — same agent, different runtime.
;; The user carries whichever format they prefer.
;; The runtime handles the rest.
;;
;; Drop this file into any repo. Any AI reads it. Full language.
;; ═══════════════════════════════════════════════════════════════
