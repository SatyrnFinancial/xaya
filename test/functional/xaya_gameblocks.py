#!/usr/bin/env python3
# Copyright (c) 2018-2019 The Xaya developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

"""Tests the "game-block" ZMQ notifications."""

from test_framework.messages import (
  COIN,
  COutPoint,
  CTransaction,
  CTxIn,
  CTxOut,
)
from test_framework.util import (
  assert_equal,
  assert_greater_than,
  assert_raises_rpc_error,
  hex_str_to_bytes,
  zmq_port,
)
from test_framework.script import (
  CScript,
  OP_TRUE,
)
from test_framework.xaya_zmq import (
  XayaZmqTest,
  ZmqSubscriber,
)

from decimal import Decimal
import json


def assertMove (obj, txid, name, move):
  """
  Utility method to assert the value of a move JSON without verifying
  the "out" field (which is unpredictable due to the change output).
  """

  assert_equal (obj["txid"], txid)
  assert_equal (obj["name"], name)
  assert_equal (obj["move"], move)


class GameBlocksTest (XayaZmqTest):

  def set_test_params (self):
    self.num_nodes = 1

  def setup_nodes (self):
    self.address = "tcp://127.0.0.1:%d" % zmq_port (1)

    args = []
    args.append ("-zmqpubgameblocks=%s" % self.address)
    args.append ("-maxgameblockattaches=10")
    args.extend (["-trackgame=%s" % g for g in ["a", "b", "other"]])
    self.add_nodes (self.num_nodes, extra_args=[args])
    self.start_nodes ()
    self.import_deterministic_coinbase_privkeys ()

    self.node = self.nodes[0]

  def run_test (self):
    # Make the checks for BitcoinTestFramework subclasses happy.
    super ().run_test ()

  def run_test_with_zmq (self, ctx):
    self.games = {}
    for g in ["a", "b", "ignored"]:
      self.games[g] = ZmqSubscriber (ctx, self.address, g)
      self.games[g].subscribe ("game-block-attach")
      self.games[g].subscribe ("game-block-detach")

    self._test_currencyIgnored ()
    self._test_register ()
    self._test_blockData ()
    self._test_multipleUpdates ()
    self._test_inputs ()
    self._test_moveWithCurrency ()
    self._test_adminCmd ()
    self._test_reorg ()
    self._test_sendUpdates ()
    self._test_maxGameBlockAttaches ()

    # After all the real tests, verify no more notifications are there.
    # This especially verifies that the "ignored" game we are subscribed to
    # has no notifications (because it is not tracked by the daemon).
    self.log.info ("Verifying that there are no unexpected messages...")
    for _, sub in self.games.items ():
      sub.assertNoMessage ()

  def _test_currencyIgnored (self):
    """
    Tests that a currency transaction does not show up as move in the
    notifications.
    """

    self.log.info ("Testing pure currency transaction...")

    addr = self.node.getnewaddress ()
    self.node.sendtoaddress (addr, 1.5)
    self.node.generate (1)

    for g in ["a", "b"]:
      _, data = self.games[g].receive ()
      assert_equal (data["moves"], [])

  def _test_register (self):
    """
    Registers test names and verifies that this already triggers
    a notification (not just name_update's).
    """

    self.log.info ("Registering names...")

    txid = self.node.name_register ("p/x", json.dumps ({"g":{"a":42}}))
    self.node.name_register ("p/y", json.dumps ({"g":{"other":False}}))
    self.node.generate (1)

    _, data = self.games["a"].receive ()
    assert_equal (len (data["moves"]), 1)
    assertMove (data["moves"][0], txid, "x", 42)

    _, data = self.games["b"].receive ()
    assert_equal (data["moves"], [])

  def _test_blockData (self):
    """
    Verifies the block-related data (parent/child hashes, rngseed) in the main
    message that is sent against the blockchain.
    """

    self.log.info ("Verifying block data...")

    parent = self.node.getbestblockhash ()
    blkHash = self.node.generate (1)[0]
    data = self.node.getblock (blkHash)
    expected = {
      "block":
        {
          "hash": blkHash,
          "parent": parent,
          "height": self.node.getblockcount (),
          "timestamp": data['time'],
          "rngseed": data['rngseed'],
        },
      "moves": []
    }

    for g in ["a", "b"]:
      _, data = self.games[g].receive ()
      assert_equal (data, expected)

  def _test_multipleUpdates (self):
    """
    Tests the case of multiple name updates in the block and multiple
    games referenced for a single name.
    """

    self.log.info ("Testing multiple updates in one block...")

    txidX = self.node.name_update ("p/x", json.dumps ({
      "stuff": "foo",
      "g":
        {
          "a": [42, False],
          "b": {"test": True},
        },
    }))
    txidY = self.node.name_update ("p/y", json.dumps ({"g":{"b":6.25}}))
    blk = self.node.generate (1)[0]

    # Get the order of our two transactions in the block, so that we can check
    # that the order in the notification matches it.
    txids = self.node.getblock (blk)["tx"][1:]
    assert_equal (set (txids), set ([txidX, txidY]))
    indX = txids.index (txidX)
    indY = txids.index (txidY)

    _, data = self.games["a"].receive ()
    assert_equal (len (data["moves"]), 1)
    assertMove (data["moves"][0], txidX, "x", [42, False])

    _, data = self.games["b"].receive ()
    assert_equal (len (data["moves"]), 2)
    assertMove (data["moves"][indX], txidX, "x", {"test": True})
    assertMove (data["moves"][indY], txidY, "y", 6.25)

  def _test_inputs (self):
    """
    Tests that spent transaction inputs are passed to the game (so that
    certain atomic transactions can be implemented).
    """

    self.log.info ("Testing for spent inputs...")
    txid = self.node.name_update ("p/x", json.dumps ({"g": {"a": "foo"}}))
    txData = self.node.getrawtransaction (txid, True)
    self.node.generate (1)

    inputs = []
    for txin in txData["vin"]:
      inputs.append ({"txid": txin["txid"], "vout": txin["vout"]})
    assert_greater_than (len (inputs), 1)

    _, data = self.games["a"].receive ()
    assert_equal (len (data["moves"]), 1)
    assert_equal (data["moves"][0]["inputs"], inputs)

    _, data = self.games["b"].receive ()
    assert_equal (data["moves"], [])

  def _test_moveWithCurrency (self):
    """
    Sends currency to predefined addresses together with a move and checks
    the "out" field produced by the notifications.
    """

    self.log.info ("Sending move with currency...")

    addr1 = self.node.getnewaddress ()
    addr2 = "dHNvNaqcD7XPDnoRjAoyfcMpHRi5upJD7p"

    # Send a move and spend coins in the same transaction.  We use a raw
    # transaction, so that we can actually send *two* outputs to the *same*
    # address.  This should work, and return it only once in the notification
    # with the total amount.  We use amounts with precision down to satoshis
    # to check this works correctly.

    hex1 = self.node.getaddressinfo (addr1)["scriptPubKey"]
    hex2 = self.node.getaddressinfo (addr2)["scriptPubKey"]
    scr1 = CScript (hex_str_to_bytes (hex1))
    scr2 = CScript (hex_str_to_bytes (hex2))

    tx = CTransaction ()
    name = self.node.name_show ("p/x")
    tx.vin.append (CTxIn (COutPoint (int (name["txid"], 16), name["vout"])))
    tx.vout.append (CTxOut (12345678, scr1))
    tx.vout.append (CTxOut (142424242, scr2))
    tx.vout.append (CTxOut (COIN, scr1))
    tx.vout.append (CTxOut (COIN // 2, CScript ([OP_TRUE])))
    tx.vout.append (CTxOut (COIN // 100, scr1))
    rawtx = tx.serialize ().hex ()

    nameOp = {
      "op": "name_update",
      "name": "p/x",
      "value": json.dumps ({"g":{"a":"move"}}),
    }
    rawtx = self.node.namerawtransaction (rawtx, 4, nameOp)["hex"]

    rawtx = self.node.fundrawtransaction (rawtx)["hex"]
    signed = self.node.signrawtransactionwithwallet (rawtx)
    assert signed["complete"]
    txid = self.node.sendrawtransaction (signed["hex"])
    self.node.generate (1)

    _, data = self.games["a"].receive ()
    assert_equal (len (data["moves"]), 1)
    assertMove (data["moves"][0], txid, "x", "move")
    out = data["moves"][0]["out"]
    assert_equal (len (out), 3)
    quant = Decimal ('1.00000000')
    assert_equal (Decimal (out[addr1]).quantize (quant), Decimal ('1.12345678'))
    assert_equal (Decimal (out[addr2]).quantize (quant), Decimal ('1.42424242'))

    _, data = self.games["b"].receive ()
    assert_equal (data["moves"], [])

  def _test_adminCmd (self):
    """
    Tests admin commands for games.
    """

    self.log.info ("Testing game admin commands...")

    # Register one of the game names already, but in a way that should
    # not trigger any admin command notifications.
    self.node.name_register ("g/a", json.dumps ({"foo": "bar"}))
    self.node.generate (1)

    for g in ["a", "b"]:
      _, data = self.games[g].receive ()
      assert_equal (data["moves"], [])
      assert "cmd" not in data

    # Now actually issue admin commands together with a move.  One of the
    # g/ names is updated, the other registered.  This makes sure that both
    # work as expected.
    self.node.name_update ("g/a", json.dumps ({
      "stuff": "ignored",
      "cmd": 42,
    }))
    self.node.name_register ("g/b", json.dumps ({
      "cmd": {"foo": "bar"},
    }))
    txid = self.node.name_update ("p/x", json.dumps ({"g":{"a":True}}))
    self.node.generate (1)

    _, data = self.games["a"].receive ()
    assert_equal (len (data["moves"]), 1)
    assertMove (data["moves"][0], txid, "x", True)
    assert_equal (data["cmd"], 42)

    _, data = self.games["b"].receive ()
    assert_equal (data["moves"], [])
    assert_equal (data["cmd"], {"foo": "bar"})

  def buildChain (self, n):
    """
    Builds a chain of length n and records the attach sequence we get
    for games a and b.
    """

    # Use a freshly generated address for the coinbases to ensure that
    # no blocks are equal on two chains.
    addr = self.node.getnewaddress ()

    blks = []
    attachA = []
    attachB = []
    for i in range (n):
      blks.extend (self.node.generatetoaddress (1, addr))
      topic, data = self.games["a"].receive ()
      assert_equal (topic, "game-block-attach json a")
      if len (attachA) > 0:
        assert_equal (attachA[-1]['block']['hash'], data['block']['parent'])
      attachA.append (data)

      topic, data = self.games["b"].receive ()
      assert_equal (topic, "game-block-attach json b")
      if len (attachB) > 0:
        assert_equal (attachB[-1]['block']['hash'], data['block']['parent'])
      attachB.append (data)

    return blks, attachA, attachB

  def verifyDetach (self, game, attach):
    """
    Verify that we receive the correct detach sequence for the given game,
    corresponding to the previously recorded attachments.
    """

    for d in reversed (attach):
      topic, data = self.games[game].receive ()
      assert_equal (topic, "game-block-detach json %s" % game)
      assert_equal (data, d)

  def verifyAttach (self, game, attach):
    """
    Verify that we receive again the recorded attach sequence for the game.
    """

    for a in attach:
      topic, data = self.games[game].receive ()
      assert_equal (topic, "game-block-attach json %s" % game)
      assert_equal (data, a)

  def addReqtoken (self, token, chain):
    """
    Adds (or changes) the given reqtoken field to the chain of attach/detach
    data blocks.
    """

    for blk in chain:
      blk['reqtoken'] = token

  def buildAndVerifyReorg (self):
    """
    Constructs a short and long chain, triggering a reorg.  Verifies the
    expected notifications and returns them.

    This is basically the reorg test, but also used for testing
    game_sendupdates.
    """

    # Start by building a long chain that we will later reorg to.  Include a
    # move (just because, not really important) and record the attach
    # notifications we get.
    self.node.name_update ("p/x", json.dumps ({"g":{"a": True}}))
    longBlks, longAttachA, longAttachB = self.buildChain (10)

    # Invalidate the first block, which should trigger detaches.
    self.node.invalidateblock (longBlks[0])
    self.verifyDetach ("a", longAttachA)
    self.verifyDetach ("b", longAttachB)

    # Build a shorter chain.
    shortBlks, shortAttachA, shortAttachB = self.buildChain (5)

    # Trigger the reorg and verify the notifications.
    self.node.reconsiderblock (longBlks[0])
    self.verifyDetach ("a", shortAttachA)
    self.verifyDetach ("b", shortAttachB)
    self.verifyAttach ("a", longAttachA)
    self.verifyAttach ("b", longAttachB)

    res = {
      "long":
        {
          "blocks": longBlks,
          "attachA": longAttachA,
          "attachB": longAttachB,
        },
      "short":
        {
          "blocks": shortBlks,
          "attachA": shortAttachA,
          "attachB": shortAttachB,
        },
    }
    return res

  def _test_reorg (self):
    """
    Produces a reorg and checks the resulting sequence of notifications
    (including those for detached blocks).
    """

    self.log.info ("Testing reorg...")
    self.buildAndVerifyReorg ()

  def _test_sendUpdates (self):
    """
    Tests on-demand notifications using game_sendupdates.
    """

    self.log.info ("Testing game_sendupdates...")

    # Build up a fork for testing.
    ancestor = self.node.getbestblockhash ()
    reorg = self.buildAndVerifyReorg ()
    assert_equal (self.node.getbestblockhash (), reorg["long"]["blocks"][-1])

    # Trigger on-demand updates in both directions for the fork.
    resA = self.node.game_sendupdates ("a", reorg["short"]["blocks"][-1])
    resB = self.node.game_sendupdates ("b", reorg["long"]["blocks"][-1],
                                       reorg["short"]["blocks"][-1])
    tokenA = resA['reqtoken']
    tokenB = resB['reqtoken']
    assert tokenA != tokenB

    # Check the return values.
    assert_equal (resA, {
      "toblock": reorg["long"]["blocks"][-1],
      "ancestor": ancestor,
      "reqtoken": tokenA,
      "steps":
        {
          "attach": 10,
          "detach": 5,
        },
    })
    assert_equal (resB, {
      "toblock": reorg["short"]["blocks"][-1],
      "ancestor": ancestor,
      "reqtoken": tokenB,
      "steps":
        {
          "attach": 5,
          "detach": 10,
        },
    })

    # Add the tokens in to the expected chains.
    self.addReqtoken (tokenA, reorg["short"]["attachA"])
    self.addReqtoken (tokenA, reorg["long"]["attachA"])
    self.addReqtoken (tokenB, reorg["short"]["attachB"])
    self.addReqtoken (tokenB, reorg["long"]["attachB"])

    # Check the updates for the games themselves.
    self.verifyDetach ("a", reorg["short"]["attachA"])
    self.verifyAttach ("a", reorg["long"]["attachA"])
    self.verifyDetach ("b", reorg["long"]["attachB"])
    self.verifyAttach ("b", reorg["short"]["attachB"])

    # Trigger updates for a game without subscribers.
    genesis = self.node.getblockhash (0)
    self.node.game_sendupdates ("other", genesis)

    # Updates for a game that is not normally tracked should still work.
    res = self.node.game_sendupdates ("ignored", genesis)
    assert_equal (res["steps"]["detach"], 0)
    for i in range (res["steps"]["attach"]):
      topic, data = self.games["ignored"].receive ()
      assert_equal (topic, "game-block-attach json ignored")
      assert_equal (data["block"]["parent"], self.node.getblockhash (i))
      assert_equal (data["block"]["hash"], self.node.getblockhash (i + 1))

    # Verify that only detaches work fine.
    tip = self.node.getbestblockhash ()
    res = self.node.game_sendupdates ("a", tip, ancestor)
    self.addReqtoken (res['reqtoken'], reorg["long"]["attachA"])
    assert_equal (res, {
      "reqtoken": res['reqtoken'],
      "toblock": ancestor,
      "ancestor": ancestor,
      "steps":
        {
          "attach": 0,
          "detach": 10,
        },
    })
    self.verifyDetach ("a", reorg["long"]["attachA"])

    # Check the case of there being no update.
    res = self.node.game_sendupdates ("a", tip)
    assert_equal (res, {
      "reqtoken": res['reqtoken'],
      "toblock": tip,
      "ancestor": tip,
      "steps":
        {
          "attach": 0,
          "detach": 0,
        },
    })

    # Verify error for invalid block hashes.
    invalidBlock = "00" * 32
    assert_raises_rpc_error (-5, "fromblock not found",
                             self.node.game_sendupdates, "a", invalidBlock)
    assert_raises_rpc_error (-5, "toblock not found",
                             self.node.game_sendupdates,
                             "a", genesis, invalidBlock)

  def _test_maxGameBlockAttaches (self):
    """
    Tests the -maxgameblockattaches option with game_sendupdates.
    """

    self.log.info ("Testing the -maxgameblockattaches limit...")

    # Build a short and a long chain, where the long chain is beyond
    # the -maxgameblockattaches limit of 10 in the test.
    ancestor = self.node.getbestblockhash ()
    shortBlks, shortAttachA, shortAttachB = self.buildChain (5)
    self.node.invalidateblock (shortBlks[0])
    self.verifyDetach ("a", shortAttachA)
    self.verifyDetach ("b", shortAttachB)
    longBlks, longAttachA, longAttachB = self.buildChain (15)

    # Request updates that go beyond the limit.
    res = self.node.game_sendupdates ("a", shortBlks[-1])
    self.addReqtoken (res['reqtoken'], shortAttachA)
    self.addReqtoken (res['reqtoken'], longAttachA)
    assert_equal (res, {
      "reqtoken": res['reqtoken'],
      "toblock": longBlks[9],
      "ancestor": ancestor,
      "steps":
        {
          "attach": 10,
          "detach": 5,
        },
    })
    self.verifyDetach ("a", shortAttachA)
    self.verifyAttach ("a", longAttachA[:10])

    # Detaches beyond the limit are fine (i.e. it only limits attaches, as
    # those are the only ones expected to be potentially very long).
    res = self.node.game_sendupdates ("a", longBlks[-1], shortBlks[-1])
    self.addReqtoken (res['reqtoken'], longAttachA)
    self.addReqtoken (res['reqtoken'], shortAttachA)
    assert_equal (res, {
      "reqtoken": res['reqtoken'],
      "toblock": shortBlks[-1],
      "ancestor": ancestor,
      "steps":
        {
          "attach": 5,
          "detach": 15,
        },
    })
    self.verifyDetach ("a", longAttachA)
    self.verifyAttach ("a", shortAttachA)


if __name__ == '__main__':
    GameBlocksTest ().main ()