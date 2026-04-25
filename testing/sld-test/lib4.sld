(define-library (lib4)
  (import (scheme base))
  (export greet)
  (begin (define (greet) 'auto-discovered)))
