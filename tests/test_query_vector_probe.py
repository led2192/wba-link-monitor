import json
import unittest

import query_vector_probe as P


class QueryVectorProbe(unittest.TestCase):
    def test_a_reply_is_read_in_its_three_shapes(self):
        self.assertEqual(P.as_vector("[3, 4]"), [0.6, 0.8])
        self.assertEqual(P.as_vector("[[3, 4]]"), [0.6, 0.8])
        self.assertEqual(P.as_vector("[[[3, 0], [3, 8]]]"), [0.6, 0.8])      # a vector a token: the mean
        self.assertIsNone(P.as_vector('{"error": "no"}'))
        self.assertIsNone(P.as_vector("not json"))

    def test_the_question_goes_with_its_prefix_and_never_the_token_in_the_output(self):
        sent, lines = [], []
        vec = [1.0] + [0.0] * 383

        def send(url, token, payload):
            sent.append((url, token, payload))
            return 200, json.dumps([vec])
        ok = P.probe("secret-token", ["how many"], local=lambda q: vec, send=send, out=lines.append)
        self.assertTrue(ok)
        self.assertEqual(sent[0][2]["inputs"], "query: how many")
        self.assertIn("multilingual-e5-small", sent[0][0])
        self.assertIn("cosine with the index's model 1.0000", lines[0])
        self.assertNotIn("secret-token", "\n".join(lines))

    def test_a_refusal_or_another_model_fails_the_probe(self):
        lines = []
        self.assertFalse(P.probe("tok-abc", ["q"], send=lambda u, t, p: (404, "Not Found"), out=lines.append))
        self.assertIn("HTTP 404", lines[0]); self.assertIn("the service said: Not Found", lines[0])
        said = []
        P.probe("tok123", ["q"], send=lambda u, t, p: (402, '{"error": "no credit for tok123"}'), out=said.append)
        self.assertIn("no credit for ***", said[0]); self.assertNotIn("tok123", said[0])
        self.assertFalse(P.probe("t", ["q"], send=lambda u, t, p: (200, json.dumps([0.5] * 768)), out=lines.append))
        other = [0.0, 1.0] + [0.0] * 382                                        # the right size, another direction
        self.assertFalse(P.probe("t", ["q"], local=lambda q: [1.0] + [0.0] * 383, send=lambda u, t, p: (200, json.dumps(other)), out=lines.append))


if __name__ == "__main__":
    unittest.main()
