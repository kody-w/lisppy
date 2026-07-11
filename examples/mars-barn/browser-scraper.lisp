;; browser-scraper.lisp — Drive the vOS headless browser from LisPy
;;
;; The vOS browser engine fetches URLs, parses HTML into a virtual DOM,
;; and renders results in a GUI iframe window. This program drives it.
;;
;; Architecture:
;;   LisPy (this program)
;;     → browser-open (fetch + DOMParser = virtual DOM)
;;     → browser-read (CSS selector query on virtual DOM)
;;     → GUI iframe shows the rendered page
;;
;; Usage:
;;   In vOS terminal: paste this program
;;   Via CLI: node tools/vos-headless.js --script this-file.lisp

(begin
  (display "=== LisPy Browser Scraper ===")
  (newline)

  ;; Open a page — engine fetches it, parses it, renders it
  (browser-open "http://localhost:8787/player.html")
  (display (string-append "Page: " (browser-title)))
  (newline)

  ;; Read structured data from the virtual DOM
  (define cards (browser-read-all ".cart-card h3"))
  (display (string-append "Found " (number->string (length cards)) " strategies:"))
  (newline)
  (for-each (lambda (card)
    (display (string-append "  • " card))
    (newline))
  cards)

  ;; Extract all links from the page
  (define links (browser-links))
  (display (string-append "Links: " (number->string (length links))))
  (newline)

  ;; Query structured element data
  (define heading (browser-query "h2"))
  (display (string-append "Heading: " (get heading "text")))
  (newline)

  ;; Interact with the rendered page
  (browser-type "#pasteArea" "(set! isru_alloc 0.5)")
  (browser-click ".load-btn")
  (display "Sim started via browser automation!")
  (newline)

  ;; External profile example only. This repository parses the source but
  ;; cannot prove browser, headless, or hardware execution parity.
  (display "Code is data. The browser is an expression.")
  (newline))
