;;; ============================================================
;;; gen_cpp_unicode_tables.scm
;;;
;;; Generates cppscheme2's Unicode case-mapping + classification tables.
;;; (Formerly the Python tool gen_unicode_tables.py, now retired.)
;;;
;;; Policy (project decision "B3"): the host's Unicode database is THE
;;; standard.  pyscheme delegates its char/string operations straight to
;;; the host Python (str.upper/lower/casefold, isalpha/isdigit/isspace/
;;; isupper/islower); this script dumps the SAME data to C++ so cppscheme2
;;; stays in parity.  Running it *inside pyscheme* therefore reads from
;;; exactly the source of truth the policy names -- no second Unicode
;;; database is involved.  Re-run after a host Unicode-version bump.
;;;
;;; The mappings are per-codepoint and match pyscheme's char/string ops
;;; EXACTLY -- except context-sensitive rules (notably Greek final sigma),
;;; which a per-codepoint table cannot express; r7rs-tests is lenient there.
;;;
;;; Usage:   python -m pyscheme tools/gen_cpp_unicode_tables.scm
;;;          (run from the 3PyScheme directory)
;;;
;;; Output directory defaults to ../4CPPScheme2/src; override with the
;;; CPPSCHEME2_SRC environment variable.  The Unicode version stamped into
;;; the banner comes from (unicode-version), pyscheme's host Unicode
;;; database version -- the same database these tables are dumped from.
;;;
;;; Writes <src>/unicode_tables.h and <src>/unicode_tables.cpp.
;;; ============================================================

(define max-cp #x110000)
(define sur-lo #xD800)
(define sur-hi #xDFFF)

(define (surrogate? cp) (and (>= cp sur-lo) (<= cp sur-hi)))

;; Codepoints of a string, as a list of integers.
(define (codepoints s) (map char->integer (string->list s)))

;; "0x" + uppercase hex (number->string yields lowercase hex).
(define (hex n) (string-append "0x" (string-upcase (number->string n 16))))

;; ---- gathering -------------------------------------------------------

;; Return (upper lower fold), each a list of (cp . (result-codepoints ...))
;; for every codepoint whose full mapping differs from itself.  We walk
;; downward and cons, which leaves each list sorted ascending by cp -- the
;; order the C++ binary search requires.  Full mappings can be 1..3
;; codepoints, so we go through string-upcase/-downcase/-foldcase on a
;; one-character string rather than char-upcase (which can only return a
;; single character and so cannot express e.g. szlig -> "SS").
(define (gather-case)
  (let loop ((cp (- max-cp 1)) (up '()) (lo '()) (fo '()))
    (cond
      ((< cp 0) (list up lo fo))
      ((surrogate? cp) (loop (- cp 1) up lo fo))
      (else
       (let* ((s (string (integer->char cp)))
              (u (string-upcase s))
              (l (string-downcase s))
              (f (string-foldcase s)))
         (loop (- cp 1)
               (if (string=? u s) up (cons (cons cp (codepoints u)) up))
               (if (string=? l s) lo (cons (cons cp (codepoints l)) lo))
               (if (string=? f s) fo (cons (cons cp (codepoints f)) fo))))))))

;; Compress a per-character predicate into a sorted list of (lo . hi) ranges.
(define (gather-ranges pred?)
  (let loop ((cp 0) (start #f) (acc '()))
    (cond
      ((>= cp max-cp)
       (reverse (if start (cons (cons start (- max-cp 1)) acc) acc)))
      (else
       (let ((on (and (not (surrogate? cp)) (pred? (integer->char cp)))))
         (cond
           ((and on (not start)) (loop (+ cp 1) cp acc))
           ((and (not on) start)
            (loop (+ cp 1) #f (cons (cons start (- cp 1)) acc)))
           (else (loop (+ cp 1) start acc))))))))

;; ---- emitting --------------------------------------------------------

;; One CaseEntry initializer: {0xCP,n,{0xR0,0xR1,0xR2}},
(define (case-cell cp res)
  (let* ((n   (length res))
         (pad (append res '(0 0 0)))
         (a   (car pad))
         (b   (cadr pad))
         (c   (caddr pad)))
    (string-append "{" (hex cp) "," (number->string n) ",{"
                   (hex a) "," (hex b) "," (hex c) "}},")))

;; One Range initializer: {0xLO,0xHI},
(define (range-cell lo hi)
  (string-append "{" (hex lo) "," (hex hi) "},"))

;; Pack cell strings into <=96-column indented lines; return the lines.
(define (wrap-cells cells)
  (let loop ((cs cells) (buf "  ") (out '()))
    (cond
      ((null? cs)
       (reverse (if (> (string-length buf) 2) (cons buf out) out)))
      (else
       (let ((cell (car cs)))
         (if (> (+ (string-length buf) (string-length cell)) 96)
             (loop (cdr cs) (string-append "  " cell) (cons buf out))
             (loop (cdr cs) (string-append buf cell) out)))))))

(define (emit-case-array name entries)
  (append
    (list (string-append "static const CaseEntry " name "[] = {"))
    (wrap-cells (map (lambda (e) (case-cell (car e) (cdr e))) entries))
    (list "};"
          (string-append "static const int " name "_count = "
                         (number->string (length entries)) ";"))))

(define (emit-range-array name ranges)
  (append
    (list (string-append "static const Range " name "[] = {"))
    (wrap-cells (map (lambda (r) (range-cell (car r) (cdr r))) ranges))
    (list "};"
          (string-append "static const int " name "_count = "
                         (number->string (length ranges)) ";"))))

;; ---- file writing ----------------------------------------------------

;; Write a list of lines to a binary port joined by "\n" (no trailing
;; newline added beyond what the list itself supplies).  We use a binary
;; port + string->utf8 so the bytes are exactly UTF-8 with LF endings;
;; pyscheme's textual output ports translate "\n" to the platform newline
;; (CRLF on Windows), which we do not want in a committed source file.
(define (write-lines port lines)
  (let loop ((ls lines) (first #t))
    (when (pair? ls)
      (unless first (write-bytevector (string->utf8 "\n") port))
      (write-bytevector (string->utf8 (car ls)) port)
      (loop (cdr ls) #f))))

(define (write-file path lines)
  (let ((port (open-binary-output-file path)))
    (write-lines port lines)
    (close-port port)))

(define (warn s) (write-string s (current-error-port)))

;; ---- main ------------------------------------------------------------

(define src (or (get-environment-variable "CPPSCHEME2_SRC") "../4CPPScheme2/src"))
(define uver (unicode-version))

(define banner-1
  "// GENERATED by 3PyScheme/tools/gen_cpp_unicode_tables.scm -- DO NOT EDIT.")
(define banner-2
  (string-append "// Source: pyscheme host char/string ops, Unicode " uver "."))

;; The shared binary-search helpers + public entry points, verbatim.
(define cpp-helpers "
int map_case(char32_t cp, const CaseEntry* tab, int n, char32_t out[3])
   {
   int lo = 0, hi = n - 1;
   while (lo <= hi)
      {
      int mid = (lo + hi) / 2;
      if (tab[mid].cp == cp)
         {
         for (int i = 0; i < tab[mid].n; ++i) out[i] = tab[mid].r[i];
         return tab[mid].n;
         }
      if (tab[mid].cp < cp) lo = mid + 1; else hi = mid - 1;
      }
   out[0] = cp;
   return 1;
   }

bool in_ranges(char32_t cp, const Range* tab, int n)
   {
   int lo = 0, hi = n - 1;
   while (lo <= hi)
      {
      int mid = (lo + hi) / 2;
      if (cp < tab[mid].lo) hi = mid - 1;
      else if (cp > tab[mid].hi) lo = mid + 1;
      else return true;
      }
   return false;
   }
}  // namespace

namespace unicode {
int upcase(char32_t cp, char32_t out[3]) { return map_case(cp, kUpper, kUpper_count, out); }
int downcase(char32_t cp, char32_t out[3]) { return map_case(cp, kLower, kLower_count, out); }
int foldcase(char32_t cp, char32_t out[3]) { return map_case(cp, kFold, kFold_count, out); }
bool is_alpha(char32_t cp) { return in_ranges(cp, kAlpha, kAlpha_count); }
bool is_digit(char32_t cp) { return in_ranges(cp, kDigit, kDigit_count); }
bool is_space(char32_t cp) { return in_ranges(cp, kSpace, kSpace_count); }
bool is_upper(char32_t cp) { return in_ranges(cp, kUpperC, kUpperC_count); }
bool is_lower(char32_t cp) { return in_ranges(cp, kLowerC, kLowerC_count); }
}
")

(warn "Gathering case mappings...\n")
(define case-tables (gather-case))
(define upper (car case-tables))
(define lower (cadr case-tables))
(define fold  (caddr case-tables))

(warn "Gathering classification ranges...\n")
(define alpha   (gather-ranges char-alphabetic?))
(define digit   (gather-ranges char-numeric?))     ; host isdigit
(define space   (gather-ranges char-whitespace?))
(define upper-c (gather-ranges char-upper-case?))
(define lower-c (gather-ranges char-lower-case?))

;; ---- header ----
(write-file
  (string-append src "/unicode_tables.h")
  (list
    banner-1 banner-2 ""
    "#pragma once"
    "#include <cstdint>"
    ""
    "// Unicode case mapping + classification, matching pyscheme's host"
    (string-append "// char/string operations (Unicode " uver ").")
    "namespace unicode"
    "{"
    "// Full case mapping of cp into out[0..n); returns n (1..3).  Unmapped"
    "// codepoints yield themselves (n==1).  char-level ops use n==1 only."
    "int upcase(char32_t cp, char32_t out[3]);"
    "int downcase(char32_t cp, char32_t out[3]);"
    "int foldcase(char32_t cp, char32_t out[3]);"
    "bool is_alpha(char32_t cp);   // host str.isalpha"
    "bool is_digit(char32_t cp);   // host str.isdigit"
    "bool is_space(char32_t cp);   // host str.isspace"
    "bool is_upper(char32_t cp);   // host str.isupper"
    "bool is_lower(char32_t cp);   // host str.islower"
    "const char* version();   // Unicode database version, e.g. \"16.0.0\""
    "}"
    ""))

;; ---- source ----
(write-file
  (string-append src "/unicode_tables.cpp")
  (append
    (list
      banner-1 banner-2 ""
      "#include \"unicode_tables.h\""
      "#include <algorithm>"
      ""
      "namespace {"
      "struct CaseEntry { char32_t cp; uint8_t n; char32_t r[3]; };"
      "struct Range { char32_t lo, hi; };"
      "")
    (emit-case-array "kUpper" upper)
    (emit-case-array "kLower" lower)
    (emit-case-array "kFold" fold)
    (list "")
    (emit-range-array "kAlpha" alpha)
    (emit-range-array "kDigit" digit)
    (emit-range-array "kSpace" space)
    (emit-range-array "kUpperC" upper-c)
    (emit-range-array "kLowerC" lower-c)
    (list ""
          cpp-helpers
          (string-append
            "namespace unicode { const char* version() { return \""
            uver "\"; } }")
          "")))

(warn (string-append
       "Wrote unicode_tables.{h,cpp} to " src "\n  Unicode " uver
       " | upper=" (number->string (length upper))
       " lower=" (number->string (length lower))
       " fold=" (number->string (length fold))
       " | alpha=" (number->string (length alpha))
       " digit=" (number->string (length digit))
       " space=" (number->string (length space))
       " upperC=" (number->string (length upper-c))
       " lowerC=" (number->string (length lower-c))
       " ranges\n"))
