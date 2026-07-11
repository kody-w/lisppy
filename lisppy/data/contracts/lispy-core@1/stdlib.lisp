;; core-stdlib.lisp — portable lispy-core@1 combinators

(define (identity x) x)

(define (constantly x) (lambda args x))

(define (complement fn) (lambda (x) (not (fn x))))

(define (partial fn . args)
  (lambda rest (apply fn (append args rest))))
